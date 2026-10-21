"""lifecrm CLI. Every mutation routes through the domain layer; this file owns
transaction boundaries (commit/rollback) and formatting only.

Set LIFECRM_ROOT to run against another project root (used by tests and by
restores onto a fresh machine).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import typer

from . import CONTRACT_VERSION, SCHEMA_VERSION, __version__
from . import contract as contract_mod
from . import db as db_mod
from .config import PROJECT_ROOT, Config, ConfigError, load_config
from .domain import corrections, entities as entities_mod, reconcile
from .domain.sources import content_hash_text
from .domain.util import DomainError

app = typer.Typer(help="Life CRM — personal life database.", no_args_is_help=True)
entities_app = typer.Typer(help="Entity resolution and curation.", no_args_is_help=True)
entry_app = typer.Typer(help="Entry corrections.", no_args_is_help=True)
link_app = typer.Typer(help="Typed links between entries.", no_args_is_help=True)
categories_app = typer.Typer(help="Category reference table.", no_args_is_help=True)
kinds_app = typer.Typer(help="Entry-kind reference table.", no_args_is_help=True)
admin_app = typer.Typer(help="Destructive administrative commands.", no_args_is_help=True)
@app.callback()
def _root_option(
        root: str = typer.Option(
            None, "--root", metavar="DIR",
            help="Run against another project root (a demo, a restore target). "
                 "Same as setting LIFECRM_ROOT.")) -> None:
    # Set the env var rather than threading a path through every command:
    # subprocesses (dashboard's datasette) and __main__'s error path agree.
    if root:
        os.environ["LIFECRM_ROOT"] = str(Path(root).expanduser().resolve())


app.add_typer(entities_app, name="entities")
app.add_typer(entry_app, name="entry")
app.add_typer(link_app, name="link")
app.add_typer(categories_app, name="categories")
app.add_typer(kinds_app, name="kinds")
app.add_typer(admin_app, name="admin")


def _cfg() -> Config:
    root = os.environ.get("LIFECRM_ROOT")
    try:
        return load_config(Path(root) if root else None)
    except ConfigError as e:
        _fail(str(e))


def _conn(cfg: Config, readonly: bool = False) -> sqlite3.Connection:
    if not cfg.db_path.exists():
        _fail(f"database not found at {cfg.db_path} — run: python -m lifecrm init")
    try:
        conn = db_mod.connect(cfg.db_path, readonly=readonly)
    except sqlite3.OperationalError as e:
        _fail(db_error_line(cfg.db_path, e))
    if not readonly:
        pending = db_mod.pending_migrations(conn)
        if pending:
            _fail(f"database schema v{db_mod.schema_version(conn)} is behind code "
                  f"(pending: {', '.join(str(v) for v, _ in pending)}) — "
                  f"run: python -m lifecrm migrate")
    return conn


def _fail(msg: str) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(1)


def db_error_line(db_path: Path, e: sqlite3.OperationalError) -> str:
    """One line for the database errors an operator can act on."""
    text = str(e)
    if "locked" in text or "busy" in text:
        return (f"{db_path} is locked by another program (a DB browser, a "
                f"running command, OneDrive mid-sync?) — close it and re-run")
    if "unable to open" in text:
        return f"cannot open {db_path} ({text}) — check the path and permissions"
    return f"{db_path}: {text}"


def _needs_attention(results: list[dict]) -> None:
    """Exit 1 when any file failed or could not be archived, so the scheduled
    task reports failure instead of a success that hides a stuck inbox."""
    bad = [r for r in results if r.get("status") == "failed" or r.get("archive_failed")]
    if bad:
        typer.echo(f"error: {len(bad)} file(s) need attention: "
                   f"{', '.join(r['file'] for r in bad)} — see the lines above; "
                   f"failed files stay in the inbox for the next run", err=True)
        raise typer.Exit(1)


def _echo_json(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False))


def _commit_or_rollback(conn: sqlite3.Connection, fn):
    """Run fn, commit on success, roll back on DomainError — and always close
    the connection. Leaked handles keep the DB file open, which on Windows
    blocks restore's file swap."""
    try:
        result = fn()
        conn.commit()
        return result
    except DomainError as e:
        conn.rollback()
        _fail(str(e))
    finally:
        conn.close()


# ---------------------------------------------------------------- init / migrate

@app.command()
def init() -> None:
    """Create data directories and a fresh database."""
    cfg = _cfg()
    for d in (cfg.data_dir, cfg.staging_dir, cfg.transcripts_dir, cfg.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    if cfg.db_path.exists():
        typer.echo(f"database already exists: {cfg.db_path}")
        raise typer.Exit(0)
    from .domain.util import now_utc
    db_mod.init_db(cfg.db_path, now_utc())
    typer.echo(f"initialized {cfg.db_path} (schema v{SCHEMA_VERSION}, "
               f"contract v{CONTRACT_VERSION})")


@app.command()
def migrate() -> None:
    """Apply pending schema migrations (snapshots the DB first)."""
    cfg = _cfg()
    if not cfg.db_path.exists():
        _fail(f"database not found at {cfg.db_path} — run: python -m lifecrm init")
    conn = db_mod.connect(cfg.db_path)
    try:
        pending = db_mod.pending_migrations(conn)
        if not pending:
            typer.echo(f"schema v{db_mod.schema_version(conn)} — up to date")
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        snap = cfg.backups_dir / f"pre-migration-{ts}.db"
        applied = db_mod.migrate(conn, snapshot_to=snap)
        typer.echo(f"applied migrations: {', '.join(map(str, applied))} "
                   f"(pre-migration snapshot: {snap})")
    finally:
        conn.close()


# ---------------------------------------------------------------- import / transcribe

@app.command("import")
def import_cmd(
    file: str = typer.Argument(None, help="Text file to import; omit with --stdin"),
    stdin: bool = typer.Option(False, "--stdin", help="Read transcript text from stdin"),
    date: str = typer.Option(None, "--date", help="Journal date YYYY-MM-DD"),
    source_type: str = typer.Option("pasted", "--type",
                                    help="pasted | phone_transcript | manual"),
    recorded_at: str = typer.Option(None, "--recorded-at", help="ISO datetime"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Import a text transcript (pasted or phone-generated) as a first-class source."""
    from .importer import import_text
    if stdin == bool(file):
        _fail("give exactly one of FILE or --stdin")
    if stdin:
        # A pipe from Windows PowerShell can carry a BOM; strip it like every
        # file reader here does.
        text = sys.stdin.read().removeprefix("\N{ZERO WIDTH NO-BREAK SPACE}")
        path = None
    else:
        p = Path(file)
        if not p.exists():
            _fail(f"file not found: {p}")
        try:
            # utf-8-sig, like every sibling reader: Notepad and PowerShell's
            # Out-File write a BOM that would otherwise land in the transcript.
            text = p.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as e:
            _fail(f"{p.name} is not UTF-8 text (byte {e.start}: {e.reason}) — "
                  f"re-save it as UTF-8")
        path = str(p)
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(
        conn, lambda: import_text(conn, cfg, text, journal_date=date,
                                  source_type=source_type, recorded_at=recorded_at,
                                  path=path))
    for w in result["warnings"]:
        typer.echo(w, err=True)
    if as_json:
        _echo_json(result)
    else:
        typer.echo(f"imported source id={result['source_id']} for "
                   f"{result['journal_date']} ({result['chars']} chars, "
                   f"{result['segments']} segments)")


@app.command()
def transcribe(
    file: str = typer.Option(None, "--file", help="Transcribe one specific file"),
    date: str = typer.Option(None, "--date", help="Override journal date YYYY-MM-DD"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Transcribe new audio in the OneDrive inbox with local faster-whisper."""
    from .transcribe import scan_inbox, transcribe_inbox
    cfg = _cfg()
    conn = _conn(cfg)
    try:
        only = Path(file) if file else None
        if only and not only.exists():
            _fail(f"file not found: {only}")
        if not only and not scan_inbox(cfg):
            typer.echo("inbox empty — nothing to transcribe")
            return
        results = transcribe_inbox(conn, cfg, only_file=only, date_override=date)
    finally:
        conn.close()
    if as_json:
        _echo_json(results)
        _needs_attention(results)
        return
    for r in results:
        for w in r.get("warnings", []):
            typer.echo(w, err=True)
        if r["status"] == "transcribed":
            typer.echo(f"{r['file']}: source id={r['source_id']} "
                       f"({r['journal_date']}, {r['duration_sec']}s, "
                       f"{r['segments']} segments)")
        else:
            typer.echo(f"{r['file']}: {r['status']}")
    _needs_attention(results)


@app.command()
def intake(as_json: bool = typer.Option(False, "--json")) -> None:
    """Process both inboxes: dictated text notes first (no whisper load), then
    audio. This is the command the scheduled task runs."""
    from .transcribe import (intake_text_inbox, scan_inbox, scan_text_inbox,
                             transcribe_inbox)
    cfg = _cfg()
    conn = _conn(cfg)
    try:
        results = []
        if scan_text_inbox(cfg):
            results += intake_text_inbox(conn, cfg)
        if scan_inbox(cfg):
            results += transcribe_inbox(conn, cfg)
    finally:
        conn.close()
    if not results:
        typer.echo("both inboxes empty — nothing to intake")
        return
    if as_json:
        _echo_json(results)
        _needs_attention(results)
        return
    for r in results:
        for w in r.get("warnings", []):
            typer.echo(w, err=True)
        if r["status"] in ("transcribed", "imported"):
            typer.echo(f"{r['file']}: source id={r['source_id']} "
                       f"({r['journal_date']}, {r['segments']} segments)")
        else:
            typer.echo(f"{r['file']}: {r['status']}")
    _needs_attention(results)


# ---------------------------------------------------------------- ingest / apply

@app.command()
def ingest(
    draft_file: str = typer.Argument(..., help="Draft contract JSON file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate only"),
) -> None:
    """Validate a draft and reconcile it into a reviewable diff (never destructive)."""
    cfg = _cfg()
    p = Path(draft_file)
    if not p.exists():
        _fail(f"file not found: {p}")
    try:
        # utf-8-sig: tolerate the BOM that Notepad and Windows PowerShell's
        # Set-Content/Out-File prepend to 'utf8' files.
        draft = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        _fail(f"{p.name} is not valid JSON: {e}")

    conn = _conn(cfg)
    try:
        texts: dict[int, str] = {}
        inline_text = None
        srcs = draft.get("sources")
        if isinstance(srcs, list):
            for src in srcs:
                if not isinstance(src, dict):
                    continue
                if isinstance(src.get("source_id"), int):
                    row = conn.execute(
                        "SELECT text FROM transcripts WHERE source_id=? "
                        "ORDER BY id DESC LIMIT 1", (src["source_id"],)).fetchone()
                    if row:
                        texts[src["source_id"]] = row["text"]
                    elif conn.execute("SELECT 1 FROM sources WHERE id=?",
                                      (src["source_id"],)).fetchone():
                        _fail(f"source id={src['source_id']} has no transcript")
                elif isinstance(src.get("text"), str):
                    inline_text = src["text"]

        errors = contract_mod.validate_draft(conn, draft, texts, inline_text)
        if errors:
            typer.echo(f"draft invalid ({len(errors)} error(s)):", err=True)
            for e in errors:
                typer.echo(f"  {e}", err=True)
            raise typer.Exit(1)
        if dry_run:
            typer.echo(f"draft valid: {len(draft.get('entries', []))} entries, "
                       f"{len(draft.get('entry_updates', []) or [])} entry update(s), "
                       f"{len(draft.get('links', []) or [])} link(s) for "
                       f"{draft['journal_date']}")
            return

        def _do():
            from .importer import import_text
            for i, src in enumerate(draft["sources"]):
                if "text" in src:
                    imported = import_text(
                        conn, cfg, src["text"],
                        journal_date=draft["journal_date"],
                        source_type=src.get("type", "pasted"),
                        recorded_at=src.get("recorded_at"))
                    for w in imported["warnings"]:
                        typer.echo(w, err=True)
                    draft["sources"][i] = {"source_id": imported["source_id"]}
            source_ids = [s["source_id"] for s in draft["sources"]]
            all_texts = []
            for sid in sorted(source_ids):
                row = conn.execute(
                    "SELECT text FROM transcripts WHERE source_id=? "
                    "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
                all_texts.append(row["text"])
            meta = draft.get("extraction") or {}
            interface = meta.get("interface", "claude_skill")
            if interface not in ("claude_skill", "manual"):
                raise DomainError(f"extraction.interface '{interface}' must be "
                                  f"claude_skill or manual")
            run = reconcile.create_extraction_run(
                conn, source_ids=source_ids,
                provider=meta.get("provider", "claude-code"),
                model=meta.get("model"), interface=interface,
                contract_version=CONTRACT_VERSION,
                input_hash=content_hash_text("\n".join(all_texts)))
            diff = reconcile.build_diff(conn, draft, run)
            cfg.staging_dir.mkdir(parents=True, exist_ok=True)
            diff_path = cfg.staging_dir / (
                f"diff-{draft['journal_date']}-{run['uuid'][:8]}.json")
            diff_path.write_text(json.dumps(diff, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
            return diff, diff_path

        try:
            diff, diff_path = _do()
            conn.commit()
        except DomainError as e:
            conn.rollback()
            _fail(str(e))
    finally:
        conn.close()
    c = diff["counts"]
    typer.echo(f"diff for {diff['journal_date']} "
               f"({len(diff['source_ids'])} source(s)): "
               f"{c['add']} add, {c['update']} update, "
               f"{c['possible_duplicate']} possible duplicate(s), "
               f"{c['retract_proposals']} retract proposal(s), "
               f"{c['entry_updates']} cross-day update(s), {c['links']} link(s), "
               f"{c['unchanged_confirmed']} unchanged confirmed, "
               f"{c['requires_decision']} requiring a decision")
    typer.echo(f"review, adjust accepted flags if needed, then apply:")
    typer.echo(f"  python -m lifecrm apply \"{diff_path}\"")


def _mark_applied(p: Path) -> Path | None:
    """Rename a diff once it has been applied.

    Belt to the fingerprint's braces: the diff is now refused on content, but
    an applied diff should also stop *looking* applicable in the folder and in
    tab-completion, and drop out of doctor's `staging/*.json` staleness sweep.
    Renaming happens after the commit, so a failure here is a warning, never a
    lost apply.
    """
    target = p.with_name(p.name + ".applied")
    n = 2
    while target.exists():
        target = p.with_name(f"{p.name}.applied.{n}")
        n += 1
    try:
        p.rename(target)
    except OSError as e:
        typer.echo(f"warning: applied, but could not rename {p.name}: {e}", err=True)
        return None
    return target


@app.command()
def apply(diff_file: str = typer.Argument(..., help="Diff JSON from ingest"),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Apply a reviewed diff transactionally. Applying is the confirmation."""
    cfg = _cfg()
    p = Path(diff_file)
    if not p.exists():
        _fail(f"file not found: {p}")
    try:
        diff = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        _fail(f"{p.name} is not valid JSON: {e}")
    conn = _conn(cfg)
    summary = _commit_or_rollback(conn, lambda: reconcile.apply_diff(conn, diff))
    applied_as = _mark_applied(p)
    if as_json:
        _echo_json(summary)
    else:
        typer.echo(f"applied {diff['journal_date']}: "
                   f"{summary['added']} added, {summary['updated']} updated, "
                   f"{summary['retracted']} retracted, "
                   f"{summary['closed']} loop(s) closed, "
                   f"{summary['links_created']} link(s), "
                   f"{summary['entities_created']} new entities, "
                   f"{summary['day_measurements']} day measurements"
                   + (f", {summary['skipped_decisions']} skipped (needs decision)"
                      if summary['skipped_decisions'] else "")
                   + (f", {summary['skipped_duplicates']} skipped duplicate(s)"
                      if summary['skipped_duplicates'] else ""))
        if applied_as is not None:
            typer.echo(f"  {p.name} -> {applied_as.name}")


# ---------------------------------------------------------------- status / sql

def _local_today(cfg: Config) -> datetime:
    now = datetime.now(ZoneInfo(cfg.timezone))
    if now.hour < cfg.day_rollover_hour:
        now -= timedelta(days=1)
    return now


def _streak(conn: sqlite3.Connection, cfg: Config) -> int:
    dates = {r["journal_date"] for r in conn.execute(
        "SELECT DISTINCT journal_date FROM entries WHERE status='active'")}
    day = _local_today(cfg)
    if day.strftime("%Y-%m-%d") not in dates:
        day -= timedelta(days=1)
    streak = 0
    while day.strftime("%Y-%m-%d") in dates:
        streak += 1
        day -= timedelta(days=1)
    return streak


@app.command()
def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """Pending audio, un-ingested sources, open drafts, streak."""
    from .transcribe import scan_inbox
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        _status_body(conn, cfg, as_json)
    finally:
        conn.close()


def _status_body(conn: sqlite3.Connection, cfg: Config, as_json: bool) -> None:
    from .transcribe import scan_inbox, scan_text_inbox
    pending_audio = [p.name for p in scan_inbox(cfg)]
    pending_text = [p.name for p in scan_text_inbox(cfg)]
    unprocessed = [dict(r) for r in conn.execute(
        "SELECT s.id AS source_id, s.journal_date, s.type, s.created_at, "
        "MAX(LENGTH(t.text)) AS chars "
        "FROM sources s JOIN transcripts t ON t.source_id=s.id "
        "WHERE NOT EXISTS (SELECT 1 FROM extraction_run_sources rs "
        "                  JOIN extraction_runs r ON r.id=rs.extraction_run_id "
        "                  WHERE rs.source_id=s.id AND r.status='applied') "
        "GROUP BY s.id ORDER BY s.journal_date DESC, s.id DESC")]
    open_runs = [dict(r) for r in conn.execute(
        "SELECT r.uuid, r.status, r.started_at, "
        "GROUP_CONCAT(rs.source_id) AS source_ids FROM extraction_runs r "
        "LEFT JOIN extraction_run_sources rs ON rs.extraction_run_id=r.id "
        "WHERE r.status IN ('draft','reviewed') GROUP BY r.id ORDER BY r.id DESC")]
    last_row = conn.execute(
        "SELECT MAX(journal_date) AS d FROM entries WHERE status='active'").fetchone()
    today_str = _local_today(cfg).strftime("%Y-%m-%d")
    open_loops = []
    for r in conn.execute(
            "SELECT e.uuid, e.title, k.name AS kind, e.journal_date, e.due_at "
            "FROM entries e JOIN entry_kinds k ON k.id=e.kind_id "
            "WHERE k.name IN ('task','plan') AND e.status='active' "
            "AND e.closed_at IS NULL "
            "ORDER BY (e.due_at IS NULL), e.due_at, e.journal_date"):
        loop = dict(r)
        loop["overdue"] = bool(r["due_at"]) and r["due_at"][:10] < today_str
        open_loops.append(loop)
    try:
        health_latest = [dict(r) for r in conn.execute(
            "SELECT metric, MAX(journal_date) AS latest, COUNT(*) AS records "
            "FROM health_records WHERE status='active' "
            "GROUP BY metric ORDER BY metric")]
    except sqlite3.OperationalError:
        health_latest = []   # pre-v3 database; migrate will add the table
    out = {
        "today": today_str,
        "pending_audio": pending_audio,
        "pending_text": pending_text,
        "unprocessed_sources": unprocessed,
        "open_extraction_runs": open_runs,
        "open_loops": open_loops,
        "last_entry_date": last_row["d"],
        "streak_days": _streak(conn, cfg),
        "health_latest": health_latest,
    }
    if as_json:
        _echo_json(out)
        return
    typer.echo(f"today: {out['today']}   streak: {out['streak_days']} day(s)   "
               f"last entry: {out['last_entry_date'] or 'none'}")
    typer.echo(f"pending audio in inbox: {len(pending_audio)}"
               + (f" ({', '.join(pending_audio)})" if pending_audio else ""))
    typer.echo(f"pending text notes: {len(pending_text)}"
               + (f" ({', '.join(pending_text)})" if pending_text else ""))
    if unprocessed:
        typer.echo("sources awaiting extraction:")
        for s in unprocessed:
            typer.echo(f"  id={s['source_id']}  {s['journal_date']}  {s['type']}  "
                       f"{s['chars']} chars")
    else:
        typer.echo("no sources awaiting extraction")
    if open_loops:
        typer.echo(f"open loops ({len(open_loops)}):")
        for lp in open_loops:
            due = f"due {lp['due_at']}" if lp["due_at"] else "no due date"
            mark = "  OVERDUE" if lp["overdue"] else ""
            typer.echo(f"  [{lp['kind']}] {lp['title']} ({lp['journal_date']}, "
                       f"{due}){mark}")
    if health_latest:
        typer.echo("health data: " + ", ".join(
            f"{h['metric']} through {h['latest']} ({h['records']})"
            for h in health_latest))


@app.command()
def sql(query: str = typer.Argument(..., help="Read-only SQL"),
        as_json: bool = typer.Option(False, "--json")) -> None:
    """Run a read-only query (URI mode=ro + PRAGMA query_only=1)."""
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        cur = conn.execute(query)
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as e:
        _fail(str(e))
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
        return
    if not rows:
        typer.echo("(no rows)")
        return
    cols = list(rows[0].keys())
    typer.echo(" | ".join(cols))
    for r in rows:
        typer.echo(" | ".join("" if r[c] is None else str(r[c]) for c in cols))


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
    """Verify every invariant the schema cannot enforce. Non-zero exit on any
    failure; warnings for operational drift (backup age, stuck drafts)."""
    from .doctor import run_checks
    cfg = _cfg()
    if not cfg.db_path.exists():
        _fail(f"database not found at {cfg.db_path} — run: python -m lifecrm init")
    # write-capable connection: the FTS integrity-check command uses INSERT
    # syntax (it modifies nothing); no data changes are ever committed.
    conn = db_mod.connect(cfg.db_path)
    try:
        report = run_checks(conn, cfg)
        conn.rollback()
    finally:
        conn.close()
    if as_json:
        _echo_json(report)
    else:
        for c in report["checks"]:
            label = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}[c["status"]]
            typer.echo(f"{label}  {c['check']}"
                       + (f" — {c['detail']}" if c["detail"] else ""))
        typer.echo(f"{report['failures']} failure(s), "
                   f"{report['warnings']} warning(s)")
    if not report["healthy"]:
        raise typer.Exit(1)


@app.command()
def export(format: str = typer.Option(..., "--format", help="jsonl | markdown"),
           out: str = typer.Option(..., "--out", help="Output directory")) -> None:
    """Dump the archive in a form that outlives this code. Deterministic:
    consecutive exports of the same data diff cleanly."""
    from . import export as export_mod
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    out_dir = Path(out)
    try:
        if format == "jsonl":
            manifest = export_mod.export_jsonl(conn, out_dir)
            typer.echo(f"exported {sum(manifest['tables'].values())} rows across "
                       f"{len(manifest['tables'])} tables to {out_dir}")
        elif format == "markdown":
            result = export_mod.export_markdown(conn, out_dir)
            typer.echo(f"exported {result['files']} markdown files "
                       f"({result['days']} day(s), {result['entities']} entities) "
                       f"to {out_dir}")
        else:
            _fail("format must be jsonl or markdown")
    finally:
        conn.close()


# ---------------------------------------------------------------- ask

def _evidence_index(bundle: dict) -> dict:
    """id -> one-line description, for printing what an answer relied on."""
    from .recall import fmt_value
    index = {}
    for m in bundle["metrics"]:
        index[m["id"]] = (f"{m['label']}: mean {fmt_value(m['mean'], m['unit'])} "
                          f"over {m['days']} day(s), {m['origin']}")
    for e in bundle["entries"]:
        index[e["id"]] = f"{e['date']} entry: {e['title']}"
    for p in bundle["passages"]:
        index[p["id"]] = f"{p['date']} transcript (source {p['source_id']})"
    return index


@app.command()
def ask(
    question: str = typer.Argument(None, help="A question about your own data"),
    days: int = typer.Option(None, "--days",
                             help="Window when the question names no time"),
    no_llm: bool = typer.Option(False, "--no-llm",
                                help="Evidence and statistics only, never a model"),
    model: str = typer.Option(None, "--model", help="Override the local model"),
    show_evidence: bool = typer.Option(False, "--evidence",
                                       help="Print the full evidence block"),
    history: bool = typer.Option(False, "--history",
                                 help="List the 20 most recent questions instead"),
    no_log: bool = typer.Option(False, "--no-log",
                                help="Do not record the question in recall_log"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Ask a question about your own life data, answered with citations.

    Retrieval is deterministic: the window and metrics are parsed by rule and
    every fact comes back with an evidence id you can verify. A local model
    (Ollama, if installed) only phrases that evidence — it is never a source
    of facts, and its citations are checked against the evidence before you
    see them. Without a model you still get the statistics and the sources.
    """
    from . import llm as llm_mod
    from . import recall as recall_mod
    from .domain import recall as recall_domain
    cfg = _cfg()

    if history:
        conn = _conn(cfg, readonly=True)
        try:
            rows = recall_domain.recent(conn, limit=20)
        finally:
            conn.close()
        if as_json:
            _echo_json(rows)
            return
        if not rows:
            typer.echo("no questions asked yet")
            return
        for r in rows:
            mark = "  " if r["answered"] else " ?"
            typer.echo(f"{r['asked_at'][:16].replace('T', ' ')}{mark} "
                       f"{r['question']}")
        return

    if not question or not question.strip():
        _fail('ask what? e.g. python -m lifecrm ask "how did I sleep last week"')

    conn = _conn(cfg, readonly=True)
    try:
        bundle = recall_mod.build_evidence(
            conn, cfg, question, days=days,
            max_entries=int(cfg.ask.get("max_entries", 8)),
            max_passages=int(cfg.ask.get("max_passages", 4)))
    except sqlite3.Error as e:
        _fail(f"retrieval failed: {e}")
    finally:
        conn.close()

    evidence_text = recall_mod.render_evidence(bundle)
    answer_text = recall_mod.deterministic_answer(bundle)
    source = "deterministic"
    llm_state = {"available": False, "reason": "--no-llm"} if no_llm \
        else llm_mod.status(cfg)

    if not no_llm and llm_state["available"] and bundle["has_evidence"]:
        try:
            phrased = llm_mod.answer(cfg, evidence_text, question,
                                     model=model or llm_state["model"])
        except Exception as e:                      # transport, timeout, 404
            typer.echo(f"local model unavailable ({e}) — deterministic answer",
                       err=True)
            phrased = ""
        if phrased:
            answer_text = phrased
            source = f"model:{model or llm_state['model']}"

    # Both paths cite evidence ids, so both get checked the same way — the
    # printed sources are what the answer actually used, never everything
    # that was retrieved.
    citations = recall_mod.check_citations(answer_text, bundle)

    if not no_log:
        wconn = _conn(cfg)
        _commit_or_rollback(wconn, lambda: recall_domain.log_question(
            wconn, question, answered=bundle["has_evidence"],
            answer_summary=answer_text))
    if as_json:
        _echo_json({"question": question, "answer": answer_text,
                    "answer_source": source, "citations": citations,
                    "evidence": bundle, "evidence_text": evidence_text,
                    "model": llm_state})
        return

    typer.echo(answer_text)
    if source.startswith("model:"):
        if citations["unknown"]:
            typer.echo(f"warning: the model cited evidence that does not exist "
                       f"({', '.join(citations['unknown'])}) — treat those "
                       f"claims as unsupported", err=True)
        if citations["uncited"]:
            typer.echo("warning: the model cited no evidence — verify it "
                       "against --evidence", err=True)
    index = _evidence_index(bundle)
    used = citations["cited"]
    if used and not show_evidence:
        typer.echo("")
        typer.echo("sources:")
        for cid in used:
            typer.echo(f"  [{cid}] {index[cid]}")
    if show_evidence:
        typer.echo("")
        typer.echo(evidence_text)
    w = bundle["window"]
    note = "" if llm_state["available"] or no_llm else f" · {llm_state['reason']}"
    typer.echo("")
    typer.echo(f"window: {w['label']} ({w['start']} to {w['end']}) · "
               f"answer: {source}{note}")


# ---------------------------------------------------------------- dashboard

@app.command()
def dashboard(
    port: int = typer.Option(8001, "--port"),
    host: str = typer.Option("127.0.0.1", "--host",
                             help="0.0.0.0 to reach it from your phone on the LAN"),
) -> None:
    """Serve read-only dashboards (Datasette) over a snapshot of the database.

    The snapshot is taken now via the backup API, so the server never holds
    the live DB open (a held-open file would block restore's file swap on
    Windows) and every chart reads one consistent instant. Rerun to refresh;
    Ctrl+C to stop. Dashboard definitions live in dashboards\\dashboards.yml.
    """
    import importlib.util
    if importlib.util.find_spec("datasette") is None:
        _fail("datasette is not installed — run: .venv\\Scripts\\python.exe "
              "-m pip install -r requirements.txt")
    cfg = _cfg()
    # A customized copy in the active root wins; otherwise the repo default
    # (same fallback pattern as config.example.toml).
    metadata = cfg.root / "dashboards" / "dashboards.yml"
    if not metadata.exists():
        metadata = PROJECT_ROOT / "dashboards" / "dashboards.yml"
    if not metadata.exists():
        _fail(f"dashboard config not found: {metadata}")
    conn = _conn(cfg)   # write-capable connection: enforces the migration gate
    try:
        snap = cfg.data_dir / "dashboard" / "lifecrm.db"
        for stale in (snap, snap.with_name(snap.name + "-wal"),
                      snap.with_name(snap.name + "-shm")):
            stale.unlink(missing_ok=True)
        db_mod.snapshot(conn, snap)
    finally:
        conn.close()
    typer.echo("serving a snapshot taken just now — rerun to refresh the data")
    typer.echo(f"dashboards: http://{host}:{port}/-/dashboards")
    raise typer.Exit(subprocess.call(
        [sys.executable, "-m", "datasette", "serve", "-i", str(snap),
         "--metadata", str(metadata), "--host", host, "--port", str(port)]))


# ---------------------------------------------------------------- health

health_app = typer.Typer(help="Imported sensor/app health data.",
                         no_args_is_help=True)
app.add_typer(health_app, name="health")


@health_app.command("import")
def health_import(
    file: str = typer.Argument(..., help="Health Connect export (.zip or .db)"),
    apply_changes: bool = typer.Option(False, "--apply",
                                       help="Write; default is a dry-run report"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Import an Android Health Connect export. Dry-run by default: review the
    per-metric counts, date spans, and sample values (a wrong unit or date is
    visible there), then re-run with --apply. Re-importing is always safe —
    records dedupe on the source system's own record ids."""
    from . import health_connect as hc
    cfg = _cfg()
    p = Path(file)
    if not p.exists():
        _fail(f"file not found: {p}")
    try:
        scan = hc.read_export(p, cfg)
    except DomainError as e:
        _fail(str(e))
    conn = _conn(cfg)
    try:
        plan = hc.build_plan(conn, scan["records"])
        if not apply_changes:
            conn.rollback()
            if as_json:
                _echo_json(hc.plan_summary(scan, plan))
            else:
                for line in hc.report_lines(scan, plan):
                    typer.echo(line)
                typer.echo("dry run — nothing written. Re-run with --apply "
                           "to write.")
            return
        try:
            summary = hc.apply_plan(conn, plan)
            conn.commit()
        except DomainError as e:
            conn.rollback()
            _fail(str(e))
    finally:
        conn.close()
    if as_json:
        _echo_json({**hc.plan_summary(scan, plan), "applied": summary})
        return
    for line in hc.report_lines(scan, plan):
        typer.echo(line)
    typer.echo(f"applied: {summary['added']} added, {summary['updated']} "
               f"updated, {summary['refreshed']} refreshed; "
               f"{summary['identical']} identical skipped"
               + (f"; {summary['conflicts']} conflict(s) kept existing"
                  if summary["conflicts"] else ""))


@health_app.command("retract")
def health_retract(
    source_file: str = typer.Option(..., "--source-file",
                                    help="Export file name, as `health import` reported it"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Undo an import: every health record that export last wrote becomes
    status='retracted' — hidden from doctor, dashboards and ask, but still on
    disk. This is the undo for an export that landed with a wrong unit scale
    or wrong dates. Put it back with `health restore --source-file`; note that
    re-importing does NOT, since an unchanged record is skipped as identical.
    """
    from .domain import health as health_dom
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(
        conn, lambda: health_dom.retract_source_file(conn, source_file))
    _health_status_echo(result, as_json, "retracted")


@health_app.command("restore")
def health_restore(
    source_file: str = typer.Option(..., "--source-file",
                                    help="Export file name, as `health import` reported it"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Put back records hidden by `health retract --source-file`."""
    from .domain import health as health_dom
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(
        conn, lambda: health_dom.restore_source_file(conn, source_file))
    _health_status_echo(result, as_json, "restored")


def _health_status_echo(result: dict, as_json: bool, verb: str) -> None:
    if as_json:
        _echo_json(result)
        return
    detail = ", ".join(f"{m} x{n}" for m, n in sorted(result["metrics"].items()))
    typer.echo(f"{verb} {result['records']} health record(s) from "
               f"{result['source_file']}: {detail}")


# ---------------------------------------------------------------- observed tier

imports_app = typer.Typer(help="Importer runs: what came in, and how to undo it.",
                          no_args_is_help=True)
handles_app = typer.Typer(help="Machine identifiers seen in imports — and who they are.",
                          no_args_is_help=True)
app.add_typer(imports_app, name="imports")
app.add_typer(handles_app, name="handles")


def _mass_removal(plan: dict) -> str | None:
    from .domain.observations import mass_removal
    return mass_removal(plan)


def _observe_summary(scan: dict, plan: dict, prior=None) -> dict:
    return {"connector": scan["connector"], "account": scan.get("account"),
            "source_file": scan.get("source_file"),
            "source_hash": scan.get("source_hash"),
            "streams": plan["streams"], "scanned": len(scan["records"]),
            "new": len(plan["new"]), "updated": len(plan["updates"]),
            "refreshed": len(plan["refreshed"]), "identical": plan["identical"],
            "duplicates_in_export": plan["dup_incoming"],
            "conflicts": plan["conflicts"], "invalid": plan["invalid"],
            "handles": scan.get("handles", []), "unmapped": scan.get("unmapped", []),
            "skipped": scan.get("skipped", []), "facts": scan.get("facts"),
            "removed": len(plan.get("removed", [])),
            "mass_removal": _mass_removal(plan),
            "previously_imported_as": prior["uuid"] if prior else None}


def _split_handle(handle: str) -> tuple[str, str]:
    kind, sep, value = handle.partition(":")
    if not sep or not kind.strip() or not value.strip():
        _fail(f"handle must be 'kind:value' (e.g. email:jake@acme.com), got '{handle}'")
    return kind.strip(), value.strip()


@app.command()
def connectors() -> None:
    """List available connectors for the observed tier and their last import."""
    from .connectors import base as conn_base
    cfg = _cfg()
    reg = conn_base.registry()
    conn = _conn(cfg, readonly=True)
    try:
        last = {r["connector"]: r for r in conn.execute(
            "SELECT connector, MAX(started_at) started_at, COUNT(*) runs "
            "FROM imports WHERE status='applied' GROUP BY connector")}
        counts = {r["stream"]: r["c"] for r in conn.execute(
            "SELECT stream, COUNT(*) c FROM observations WHERE status='active' "
            "GROUP BY stream")}
    finally:
        conn.close()
    for cname in sorted(reg):
        c = reg[cname]
        row = last.get(cname)
        when = (f"last import {row['started_at'][:10]} ({row['runs']} run(s))"
                if row else "never imported")
        typer.echo(f"{cname:12} {when}")
        for s in c.streams:
            typer.echo(f"  {s:16} {counts.get(s, 0)} active observation(s)")


@app.command()
def observe(
    connector: str = typer.Argument(..., help="Connector name — see: lifecrm connectors"),
    target: str = typer.Argument(
        None, help="Export .zip, directory, or .json file. ios_messages: omit it "
                   "to use the iPhone backup on this machine"),
    apply_changes: bool = typer.Option(False, "--apply",
                                       help="Write; default is a dry-run report"),
    allow_mass_removal: bool = typer.Option(
        False, "--allow-mass-removal",
        help="Mirroring connectors: allow removing a large share of the archive "
             "at once (refused by default — it usually means an incomplete source)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Import an export into the observed tier (bulk records, no review gate).

    Dry-run by default: read the per-stream counts, date spans and sample rows
    — a wrong date or a misparsed body is visible there — then re-run with
    --apply. Re-importing is always safe: records dedupe on the source
    system's own ids, and only strictly newer records update an existing row.

    Nothing here can touch entries, measurements or mentions. Observed records
    are cited, never asserted.
    """
    from .connectors import base as conn_base
    from .domain import observations as obs
    cfg = _cfg()
    try:
        c = conn_base.get(connector)
        if target is None:
            finder = getattr(c, "default_target", None)
            if finder is None:
                _fail(f"{connector} needs a path to read — the export file or folder")
            p = finder(cfg)
        else:
            p = Path(target)
            if not p.exists():
                _fail(f"not found: {p}")
        scan = c.scan(p, cfg)
    except DomainError as e:
        _fail(str(e))

    conn = _conn(cfg)
    try:
        prior = None
        if scan.get("source_hash"):
            prior = conn.execute(
                "SELECT uuid, started_at FROM imports WHERE connector=? AND "
                "source_hash=? AND status='applied' ORDER BY id DESC LIMIT 1",
                (scan["connector"], scan["source_hash"])).fetchone()
        try:
            plan = obs.build_plan(conn, scan)
        except DomainError as e:
            _fail(str(e))

        if not apply_changes:
            conn.rollback()
            if as_json:
                _echo_json(_observe_summary(scan, plan, prior))
            else:
                for line in c.report_lines(scan, plan):
                    typer.echo(line)
                if prior:
                    typer.echo(f"  note: this exact file was already imported on "
                               f"{prior['started_at'][:10]} ({prior['uuid'][:8]})")
                refusal = obs.mass_removal(plan)
                if refusal and not allow_mass_removal:
                    typer.echo(f"  WILL BE REFUSED on --apply: {refusal}")
                typer.echo("dry run — nothing written. Re-run with --apply to write.")
            return

        refusal = obs.mass_removal(plan)
        if refusal and not allow_mass_removal:
            conn.rollback()
            _fail(refusal)
        snapshot = None
        if plan.get("removed"):
            # Removal is the one write here that a re-import cannot always
            # undo, so it gets the migration treatment: a snapshot first. It is
            # named like every other snapshot, so tiered retention prunes it.
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            snapshot = db_mod.snapshot(conn, cfg.backups_dir / f"lifecrm-{ts}.db")
        try:
            summary = obs.apply_plan(conn, scan, plan,
                                     allow_mass_removal=allow_mass_removal)
            conn.commit()
        except DomainError as e:
            conn.rollback()
            _fail(str(e))
    finally:
        conn.close()

    if as_json:
        _echo_json({**_observe_summary(scan, plan, prior), "applied": summary,
                    "snapshot": str(snapshot) if snapshot else None})
        return
    for line in c.report_lines(scan, plan):
        typer.echo(line)
    typer.echo(f"applied: {summary['added']} added, {summary['updated']} updated, "
               f"{summary['refreshed']} refreshed; {summary['identical']} identical "
               f"skipped"
               + (f"; {summary['removed']} removed" if summary.get("removed") else "")
               + (f" ({summary['retracted_cited']} cited by journal entries kept "
                  f"hidden, not deleted)" if summary.get("retracted_cited") else "")
               + (f"; {summary['conflicts']} conflict(s) kept existing"
                  if summary["conflicts"] else ""))
    if snapshot:
        typer.echo(f"snapshot before removing: {snapshot}")
    typer.echo(f"import {summary['import_uuid']} — undo with: "
               f"python -m lifecrm imports retract {summary['import_uuid']}")
    unresolved = _unresolved_count(cfg)
    if unresolved:
        typer.echo(f"{unresolved} unresolved handle(s) — see: "
                   f"python -m lifecrm handles list")


def _unresolved_count(cfg: Config) -> int:
    conn = _conn(cfg, readonly=True)
    try:
        return conn.execute("SELECT COUNT(*) c FROM entity_handles "
                            "WHERE entity_id IS NULL AND ignored=0").fetchone()["c"]
    finally:
        conn.close()


@imports_app.command("list")
def imports_list(
    limit: int = typer.Option(20, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Recent importer runs, newest first."""
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        rows = conn.execute(
            "SELECT i.*, (SELECT COUNT(*) FROM observations o "
            "WHERE o.import_id=i.id AND o.status='active') live "
            "FROM imports i ORDER BY i.id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    if as_json:
        _echo_json([dict(r) for r in rows])
        return
    if not rows:
        typer.echo("no imports yet — see: python -m lifecrm connectors")
        return
    for r in rows:
        counts = json.loads(r["counts_json"]) if r["counts_json"] else {}
        detail = (f"{counts.get('added', 0)} added, {counts.get('updated', 0)} "
                  f"updated" if counts else r["status"])
        typer.echo(f"{r['uuid'][:8]}  {r['started_at'][:16].replace('T', ' ')}  "
                   f"{r['connector']:10} {r['status']:9} {detail}  "
                   f"({r['live']} live)  {r['source_file'] or ''}")


@imports_app.command("retract")
def imports_retract(
    import_uuid: str = typer.Argument(..., help="Import uuid — see: lifecrm imports list"),
) -> None:
    """Undo an import: its observations become status='retracted'.

    Coarse by design. Rows this import merely refreshed or updated stay —
    import_id names the run that CREATED a row, so retracting a later snapshot
    cannot delete what an earlier one contributed.

    One-way for now: the rows keep their data, but re-importing does NOT bring
    them back (an unchanged record classifies as identical and is skipped),
    and there is no `imports restore` yet. `health restore` is the equivalent
    on the health side.
    """
    from .domain import observations as obs
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(conn, lambda: obs.retract_import(conn, import_uuid))
    typer.echo(f"retracted {result['retracted']} observation(s) from "
               f"{result['connector']} import {import_uuid[:8]} "
               f"({result['source_file'] or 'no file'})")


@handles_app.command("list")
def handles_list(
    show_all: bool = typer.Option(False, "--all", help="Include resolved and ignored"),
    limit: int = typer.Option(50, "--limit"),
    suggest: bool = typer.Option(False, "--suggest",
                                 help="Offer existing entities matching each name"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Machine identifiers seen in imports, noisiest first.

    This is the review queue that makes the observed tier answerable. Linking
    ONE handle to an entity resolves every observation that ever referenced
    it — past and future — with no backfill pass.

    --suggest matches each unresolved handle's display name (what the source
    called it — a contact card's name, for texts) against your entities and
    aliases. Suggestions only: nothing is linked until you run `handles link`.
    """
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    suggestions: dict[tuple[str, str], list[dict]] = {}
    try:
        if show_all:
            rows = conn.execute(
                "SELECT h.*, e.name entity_name FROM entity_handles h "
                "LEFT JOIN entities e ON e.id=h.entity_id "
                "ORDER BY h.observation_count DESC, h.kind, h.value LIMIT ?",
                (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT h.*, NULL entity_name FROM entity_handles h "
                "WHERE h.entity_id IS NULL AND h.ignored=0 "
                "ORDER BY h.observation_count DESC, h.kind, h.value LIMIT ?",
                (limit,)).fetchall()
        if suggest:
            for r in rows:
                if r["entity_id"] is None and r["display_name"]:
                    found = entities_mod.match(conn, r["display_name"])["matches"][:3]
                    if found:
                        suggestions[(r["kind"], r["value"])] = found
    finally:
        conn.close()
    if as_json:
        out = []
        for r in rows:
            item = dict(r)
            if suggest:
                item["suggestions"] = suggestions.get((r["kind"], r["value"]), [])
            out.append(item)
        _echo_json(out)
        return
    if not rows:
        typer.echo("no unresolved handles" if not show_all else "no handles yet")
        return
    for r in rows:
        if r["entity_name"]:
            who = f"-> {r['entity_name']} (id {r['entity_id']})"
        elif r["ignored"]:
            who = "-> ignored"
        else:
            who = "-> UNRESOLVED"
        span = f"{r['first_seen_at'][:10]}..{r['last_seen_at'][:10]}"
        typer.echo(f"{r['observation_count']:6}  {r['kind']}:{r['value']}  "
                   f"{who}  {span}  {r['display_name'] or ''}")
        for m in suggestions.get((r["kind"], r["value"]), []):
            typer.echo(f"          maybe: {m['name']} ({m['type']}, id {m['id']}) "
                       f"{m['score']:.2f} via {m['via']} -> handles link "
                       f"{r['kind']}:{r['value']} --entity {m['id']}")
    if not show_all:
        typer.echo("")
        typer.echo("link one with: python -m lifecrm handles link "
                   "<kind:value> --entity <id>")
        typer.echo("or create it:  python -m lifecrm handles link "
                   "<kind:value> --new \"<name>\"")
        if not suggest and any(r["display_name"] for r in rows):
            typer.echo("see candidate entities for named handles: "
                       "python -m lifecrm handles list --suggest")


@handles_app.command("link")
def handles_link(
    handle: str = typer.Argument(..., help="kind:value, e.g. email:jake@acme.com"),
    entity: int = typer.Option(None, "--entity", help="Existing entity id"),
    new_name: str = typer.Option(None, "--new", help="Create a new entity with this name"),
    new_type: str = typer.Option("person", "--type", help="Type for --new"),
) -> None:
    """Resolve one handle to one entity — every observation follows immediately."""
    from .domain import observations as obs
    kind, value = _split_handle(handle)
    if (entity is None) == (new_name is None):
        _fail("give exactly one of --entity <id> or --new <name>")
    cfg = _cfg()
    conn = _conn(cfg)

    def _do():
        target_id = entity
        created = None
        if new_name is not None:
            created = entities_mod.create(conn, new_type, new_name)
            target_id = created["id"]
        obs.link_handle(conn, kind, value, target_id)
        return target_id, created

    target_id, created = _commit_or_rollback(conn, _do)
    if created:
        typer.echo(f"created {new_type} \"{created['name']}\" (id {created['id']})")
    conn2 = _conn(cfg, readonly=True)
    try:
        n = conn2.execute(
            "SELECT COUNT(*) c FROM observation_participants p "
            "JOIN observations o ON o.id=p.observation_id "
            "WHERE p.handle_kind=? AND p.handle_value=? AND o.status='active'",
            (kind, value)).fetchone()["c"]
    finally:
        conn2.close()
    typer.echo(f"{kind}:{value} -> entity {target_id}; {n} observation(s) now "
               f"attributed")


@handles_app.command("ignore")
def handles_ignore(
    handle: str = typer.Argument(..., help="kind:value to stop asking about"),
) -> None:
    """Mark a handle as never worth resolving (spam, delivery notifications).
    Reversible: linking it later clears the flag."""
    from .domain import observations as obs
    kind, value = _split_handle(handle)
    cfg = _cfg()
    conn = _conn(cfg)
    _commit_or_rollback(conn, lambda: obs.ignore_handle(conn, kind, value))
    typer.echo(f"ignoring {kind}:{value}")


# ---------------------------------------------------------------- backup / restore

@app.command()
def backup() -> None:
    """Snapshot the DB (backup API) locally and to the OneDrive backups folder.
    Tiered retention: 14 daily, 12 weekly, 24 monthly, yearly indefinitely."""
    from .retention import prune_dir
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = cfg.backups_dir / f"lifecrm-{ts}.db"
    try:
        db_mod.snapshot(conn, dest)
    finally:
        conn.close()
    typer.echo(f"snapshot: {dest}")
    if cfg.onedrive_backups_dir is not None:
        try:
            cfg.onedrive_backups_dir.mkdir(parents=True, exist_ok=True)
            cloud = cfg.onedrive_backups_dir / dest.name
            shutil.copy2(dest, cloud)
            typer.echo(f"copied to: {cloud} (OneDrive is cloud storage — see docs/privacy.md)")
            removed = prune_dir(cfg.onedrive_backups_dir)
            if removed:
                typer.echo(f"retention pruned {len(removed)} cloud snapshot(s)")
        except OSError as e:
            typer.echo(f"WARNING: OneDrive copy failed: {e}", err=True)
    removed = prune_dir(cfg.backups_dir)
    if removed:
        typer.echo(f"retention pruned {len(removed)} local snapshot(s)")


@app.command()
def restore(snapshot_file: str = typer.Argument(
        None, help="Snapshot .db to restore; omit to list snapshots")) -> None:
    """Restore the DB from a snapshot (verifies integrity, backs up current first)."""
    cfg = _cfg()
    if snapshot_file is None:
        found = []
        for d in (cfg.backups_dir, cfg.onedrive_backups_dir):
            if d and d.exists():
                found += sorted(d.glob("lifecrm-*.db"), reverse=True)
        if not found:
            typer.echo("no snapshots found")
            return
        typer.echo("available snapshots (newest first):")
        for f in found:
            typer.echo(f"  {f}")
        typer.echo("restore with: python -m lifecrm restore <path>")
        return

    snap = Path(snapshot_file)
    if not snap.exists():
        _fail(f"snapshot not found: {snap}")
    check = sqlite3.connect(f"file:{snap.as_posix()}?mode=ro", uri=True)
    try:
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                _fail(f"snapshot failed integrity check: {ok}")
            row = check.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.DatabaseError as e:
            _fail(f"{snap.name} is not a readable SQLite database: {e}")
        if row is None:
            _fail("snapshot has no meta.schema_version — not a lifecrm database")
        snap_version = int(row[0])
        if snap_version > SCHEMA_VERSION:
            _fail(f"snapshot schema v{snap_version} is newer than this code "
                  f"(v{SCHEMA_VERSION}) — update the code first")
    finally:
        check.close()

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if cfg.db_path.exists():
        live = db_mod.connect(cfg.db_path)
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            pre = cfg.backups_dir / f"pre-restore-{ts}.db"
            db_mod.snapshot(live, pre)
            typer.echo(f"current database backed up to: {pre}")
        finally:
            live.close()
        for suffix in ("-wal", "-shm"):
            side = Path(str(cfg.db_path) + suffix)
            if side.exists():
                side.unlink()
    shutil.copy2(snap, cfg.db_path)

    conn = db_mod.connect(cfg.db_path)
    try:
        pending = db_mod.pending_migrations(conn)
        if pending:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            applied = db_mod.migrate(
                conn, snapshot_to=cfg.backups_dir / f"pre-migration-{ts}.db")
            typer.echo(f"applied migrations: {', '.join(map(str, applied))}")
        n = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
        typer.echo(f"restored {cfg.db_path} from {snap.name} "
                   f"(schema v{db_mod.schema_version(conn)}, {n} entries)")
    finally:
        conn.close()


# ---------------------------------------------------------------- entities

@entities_app.command("match")
def entities_match(
    names: list[str] = typer.Argument(..., help="Names to resolve"),
    entity_type: str = typer.Option(None, "--type"),
    as_json: bool = typer.Option(True, "--json/--text"),
) -> None:
    """Resolve names against canonical entities and aliases (never auto-picks)."""
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        results = [entities_mod.match(conn, n, entity_type) for n in names]
    except DomainError as e:
        _fail(str(e))
    finally:
        conn.close()
    if as_json:
        _echo_json(results)
        return
    for r in results:
        heads = ", ".join(f"{m['name']} (id={m['id']}, {m['type']}, {m['score']})"
                          for m in r["matches"]) or "-"
        typer.echo(f"{r['query']}: {r['status']}  {heads}")


@entities_app.command("list")
def entities_list(entity_type: str = typer.Option(None, "--type"),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    q = ("SELECT e.id, e.uuid, e.type, e.name, COUNT(m.entry_id) AS mentions "
         "FROM entities e LEFT JOIN mentions m ON m.entity_id=e.id "
         "WHERE e.status='active'")
    args: tuple = ()
    if entity_type:
        q += " AND e.type=?"
        args = (entity_type,)
    q += " GROUP BY e.id ORDER BY e.type, e.name"
    try:
        rows = [dict(r) for r in conn.execute(q, args)]
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
        return
    for r in rows:
        typer.echo(f"id={r['id']:<4} {r['type']:<12} {r['name']}  "
                   f"({r['mentions']} mention(s))")
    if not rows:
        typer.echo("(no entities)")


@entities_app.command("show")
def entities_show(entity_id: int, as_json: bool = typer.Option(False, "--json")) -> None:
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        try:
            e = entities_mod.get(conn, entity_id)
        except DomainError as err:
            _fail(str(err))
        aliases = [r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id=? ORDER BY alias",
            (entity_id,))]
        mentions = [dict(r) for r in conn.execute(
            "SELECT en.journal_date, en.title, en.uuid, m.role FROM mentions m "
            "JOIN entries en ON en.id=m.entry_id WHERE m.entity_id=? AND en.status='active' "
            "ORDER BY en.journal_date DESC LIMIT 50", (entity_id,))]
    finally:
        conn.close()
    out = dict(e) | {"aliases": aliases, "recent_mentions": mentions}
    if as_json:
        _echo_json(out)
        return
    typer.echo(f"id={e['id']} [{e['type']}] {e['name']} (status: {e['status']})")
    if aliases:
        typer.echo(f"aliases: {', '.join(aliases)}")
    if e["notes"]:
        typer.echo(f"notes: {e['notes']}")
    for m in mentions:
        typer.echo(f"  {m['journal_date']}  {m['title']}  ({m['role']})")


@entities_app.command("merge")
def entities_merge(src_id: int, dst_id: int) -> None:
    """Merge SRC into DST (mentions reassigned, name becomes alias, UUIDs kept)."""
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(
        conn, lambda: entities_mod.merge(conn, src_id, dst_id))
    typer.echo(f"merged '{result['src']}' into '{result['dst']}' "
               f"({result['mentions_moved']} mention(s) moved)")


@entities_app.command("alias-add")
def entities_alias_add(entity_id: int, alias: str) -> None:
    """Add an alias (e.g. a whisper misspelling) so it resolves forever."""
    cfg = _cfg()
    conn = _conn(cfg)
    _commit_or_rollback(conn, lambda: entities_mod.add_alias(conn, entity_id, alias))
    typer.echo(f"alias '{alias}' -> entity id={entity_id}")


# ---------------------------------------------------------------- entry corrections

def _entry_provenance(conn: sqlite3.Connection, entry_id: int) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT es.*, s.type AS source_type, s.journal_date AS source_date "
            "FROM entry_sources es JOIN sources s ON s.id=es.source_id "
            "WHERE es.entry_id=?", (entry_id,)):
        item = {"source_id": r["source_id"], "source_type": r["source_type"],
                "source_date": r["source_date"], "char_start": r["char_start"],
                "char_end": r["char_end"], "segment_start": r["segment_start"],
                "segment_end": r["segment_end"]}
        if r["transcript_id"] is not None and r["char_start"] is not None:
            t = conn.execute("SELECT text FROM transcripts WHERE id=?",
                             (r["transcript_id"],)).fetchone()
            if t:
                item["transcript_excerpt"] = t["text"][r["char_start"]:r["char_end"]]
        elif r["transcript_id"] is not None and r["segment_start"] is not None:
            segs = conn.execute(
                "SELECT text FROM transcript_segments WHERE transcript_id=? "
                "AND idx BETWEEN ? AND ? ORDER BY idx",
                (r["transcript_id"], r["segment_start"], r["segment_end"])).fetchall()
            item["transcript_excerpt"] = " ".join(s["text"] for s in segs)
        out.append(item)
    return out


@entry_app.command("show")
def entry_show(entry_uuid: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Entry with categories, entities, measurements, provenance, and revisions."""
    from .domain import entries as entries_dom
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        try:
            row = entries_dom.get_by_uuid(conn, entry_uuid)
            snap = entries_dom.snapshot(conn, row["id"])
        except DomainError as e:
            _fail(str(e))
        snap["provenance"] = _entry_provenance(conn, row["id"])
        snap["revisions"] = [dict(r) for r in conn.execute(
            "SELECT revised_at, actor, change_kind, note FROM entry_revisions "
            "WHERE entry_uuid=? ORDER BY id", (entry_uuid,))]
        from .domain import links as links_dom
        snap["links"] = links_dom.for_entry(conn, entry_uuid)
    finally:
        conn.close()
    if as_json:
        _echo_json(snap)
        return
    typer.echo(f"{snap['journal_date']}  [{snap['kind']}] {snap['title']} "
               f"(status: {snap['status']})")
    typer.echo(f"uuid: {snap['uuid']}")
    if snap.get("closure"):
        typer.echo(f"closed: {snap['closure']} at {snap['closed_at']}")
    cats = snap["categories"]
    typer.echo(f"categories: {cats['primary']}"
               + (f" + {', '.join(cats['secondary'])}" if cats["secondary"] else ""))
    if snap["body"]:
        typer.echo(f"body: {snap['body']}")
    for m in snap["measurements"]:
        typer.echo(f"  measurement: {m['metric']} = "
                   f"{m['numeric_value'] if m['numeric_value'] is not None else m['text_value']}"
                   f"{' ' + m['unit'] if m['unit'] else ''} [{m['status']}]")
    for e in snap["entities"]:
        typer.echo(f"  entity: {e['name']} ({e['type']}, {e['role']})")
    for p in snap["provenance"]:
        if p.get("transcript_excerpt"):
            typer.echo(f"  from transcript: \"{p['transcript_excerpt']}\"")
    for direction, rows in snap["links"].items():
        for link in rows:
            arrow = "->" if direction == "outgoing" else "<-"
            typer.echo(f"  link: {arrow} {link['type']} {arrow} "
                       f"[{link['other_date']}] {link['other_title']}")
    for r in snap["revisions"]:
        typer.echo(f"  revision: {r['revised_at']} {r['actor']} {r['change_kind']}"
                   + (f" ({r['note']})" if r["note"] else ""))


@entry_app.command("edit")
def entry_edit(
    entry_uuid: str,
    title: str = typer.Option(None, "--title"),
    body: str = typer.Option(None, "--body"),
    kind: str = typer.Option(None, "--kind"),
    time_of_day: str = typer.Option(None, "--time-of-day"),
    occurred_at: str = typer.Option(None, "--occurred-at"),
    ended_at: str = typer.Option(None, "--ended-at"),
    due_at: str = typer.Option(None, "--due-at"),
    confidence: str = typer.Option(None, "--confidence"),
    journal_date: str = typer.Option(None, "--journal-date"),
    primary_category: str = typer.Option(None, "--primary-category"),
    secondary: str = typer.Option(None, "--secondary",
                                  help="Comma-separated secondary categories"),
    note: str = typer.Option(None, "--note"),
) -> None:
    """Edit an entry (records a revision; marks it user-confirmed)."""
    changes: dict = {}
    for key, val in (("title", title), ("body", body), ("kind", kind),
                     ("time_of_day", time_of_day), ("occurred_at", occurred_at),
                     ("ended_at", ended_at), ("due_at", due_at),
                     ("confidence", confidence), ("journal_date", journal_date),
                     ("primary_category", primary_category)):
        if val is not None:
            changes[key] = val
    if secondary is not None:
        changes["secondary_categories"] = [s.strip() for s in secondary.split(",")
                                           if s.strip()]
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(
        conn, lambda: corrections.edit_entry(conn, entry_uuid, changes,
                                             actor="user", note=note))
    typer.echo(f"updated: [{result['kind']}] {result['title']} "
               f"({result['journal_date']})")


@entry_app.command("retract")
def entry_retract(entry_uuid: str,
                  reason: str = typer.Option(None, "--reason")) -> None:
    """Mark an entry retracted (reversible with `entry restore`)."""
    cfg = _cfg()
    conn = _conn(cfg)
    _commit_or_rollback(
        conn, lambda: corrections.retract_entry(conn, entry_uuid, reason=reason))
    typer.echo(f"retracted {entry_uuid}")


@entry_app.command("restore")
def entry_restore(entry_uuid: str) -> None:
    """Restore a retracted entry to active."""
    cfg = _cfg()
    conn = _conn(cfg)
    _commit_or_rollback(conn, lambda: corrections.restore_entry(conn, entry_uuid))
    typer.echo(f"restored {entry_uuid}")


@entry_app.command("merge")
def entry_merge(src_uuid: str, dst_uuid: str,
                note: str = typer.Option(None, "--note")) -> None:
    """Merge duplicate entries: SRC's data moves to DST, SRC becomes superseded.

    SRC keeps its own provenance — `entry show SRC` still names the passage it
    came from — and DST gains any source SRC cited that DST did not."""
    cfg = _cfg()
    conn = _conn(cfg)
    _commit_or_rollback(
        conn, lambda: corrections.merge_entries(conn, src_uuid, dst_uuid, note=note))
    typer.echo(f"merged {src_uuid} into {dst_uuid}")


@entry_app.command("close")
def entry_close(entry_uuid: str,
                closure: str = typer.Option(..., "--closure",
                                            help="done | dropped | obsolete"),
                at: str = typer.Option(None, "--at", help="ISO datetime (default now)"),
                note: str = typer.Option(None, "--note")) -> None:
    """Close a loop (task done, plan dropped, idea obsolete). Not retraction:
    the record stays true; the loop stops being open."""
    cfg = _cfg()
    conn = _conn(cfg)
    _commit_or_rollback(
        conn, lambda: corrections.close_entry(conn, entry_uuid, closure,
                                              closed_at=at, note=note))
    typer.echo(f"closed {entry_uuid} ({closure})")


@entry_app.command("reopen")
def entry_reopen(entry_uuid: str) -> None:
    """Reopen a closed loop."""
    cfg = _cfg()
    conn = _conn(cfg)
    _commit_or_rollback(conn, lambda: corrections.reopen_entry(conn, entry_uuid))
    typer.echo(f"reopened {entry_uuid}")


# ---------------------------------------------------------------- links

@link_app.command("add")
def link_add(from_uuid: str, to_uuid: str,
             link_type: str = typer.Option(..., "--type",
                                           help="continues | caused | resolves | "
                                                "contradicts | part_of | relates"),
             note: str = typer.Option(None, "--note")) -> None:
    """Link two entries. Direction: later continues earlier, new resolves old,
    child part_of parent, cause caused effect, newer contradicts older."""
    from .domain import links as links_dom
    cfg = _cfg()
    conn = _conn(cfg)
    created = _commit_or_rollback(
        conn, lambda: links_dom.add(conn, from_uuid, to_uuid, link_type, note=note))
    typer.echo("linked" if created else "link already exists")


@link_app.command("list")
def link_list(entry_uuid: str, as_json: bool = typer.Option(False, "--json")) -> None:
    from .domain import links as links_dom
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        result = links_dom.for_entry(conn, entry_uuid)
    finally:
        conn.close()
    if as_json:
        _echo_json(result)
        return
    for direction, rows in result.items():
        for link in rows:
            arrow = "->" if direction == "outgoing" else "<-"
            typer.echo(f"{arrow} {link['type']} {arrow} "
                       f"[{link['other_date']}] {link['other_title']}"
                       + (f"  ({link['note']})" if link["note"] else ""))
    if not result["outgoing"] and not result["incoming"]:
        typer.echo("(no links)")


@link_app.command("rm")
def link_rm(from_uuid: str, to_uuid: str,
            link_type: str = typer.Option(..., "--type")) -> None:
    from .domain import links as links_dom
    cfg = _cfg()
    conn = _conn(cfg)
    _commit_or_rollback(
        conn, lambda: links_dom.remove(conn, from_uuid, to_uuid, link_type))
    typer.echo("removed")


# ---------------------------------------------------------------- categories / kinds

def _ref_list(table: str, as_json: bool) -> None:
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, name FROM {table} ORDER BY id")]
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
    else:
        typer.echo(", ".join(r["name"] for r in rows))


def _ref_add(table: str, name: str) -> None:
    from .domain import audit
    from .domain.util import now_utc
    cfg = _cfg()
    name = name.strip().lower()
    if not name:
        _fail("name must be non-empty")
    conn = _conn(cfg)
    try:
        if conn.execute(f"SELECT 1 FROM {table} WHERE name=? COLLATE NOCASE",
                        (name,)).fetchone():
            _fail(f"'{name}' already exists in {table}")
        conn.execute(f"INSERT INTO {table} (name, created_at) VALUES (?, ?)",
                     (name, now_utc()))
        audit.log(conn, "user", "create", table, None, {"name": name})
        conn.commit()
    finally:
        conn.close()
    typer.echo(f"added '{name}' to {table}")


@categories_app.command("list")
def categories_list(as_json: bool = typer.Option(False, "--json")) -> None:
    _ref_list("categories", as_json)


@categories_app.command("add")
def categories_add(name: str) -> None:
    """Add a category (no schema migration needed)."""
    _ref_add("categories", name)


@kinds_app.command("list")
def kinds_list(as_json: bool = typer.Option(False, "--json")) -> None:
    _ref_list("entry_kinds", as_json)


@kinds_app.command("add")
def kinds_add(name: str) -> None:
    """Add an entry kind (no schema migration needed)."""
    _ref_add("entry_kinds", name)


# ---------------------------------------------------------------- admin

@admin_app.command("purge-day")
def purge_day(journal_date: str,
              i_am_sure: bool = typer.Option(False, "--i-am-sure")) -> None:
    """DESTRUCTIVE: delete every entry for a day. Routine cleanup should use
    entry retract/merge instead — this exists only for true do-overs."""
    if not i_am_sure:
        _fail("purge-day deletes data permanently; pass --i-am-sure to proceed "
              "(or use entry retract / entry merge for reversible cleanup)")
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(
        conn, lambda: corrections.purge_day(conn, journal_date))
    typer.echo(f"purged {journal_date}: {result['entries_deleted']} entries deleted")


@app.command()
def version() -> None:
    typer.echo(f"lifecrm {__version__} (schema v{SCHEMA_VERSION}, "
               f"contract v{CONTRACT_VERSION})")


# ---------------------------------------------------------------- plug-ins

def _register_command_modules() -> None:
    """Each module in lifecrm/commands/ defines `register(app)` and adds its
    own commands, so a feature that only adds commands never edits this file.
    Registered last: a plug-in may import this module's helpers (_cfg, _conn,
    _fail, ...), which all exist by now."""
    import importlib
    import pkgutil

    from . import commands as pkg

    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        register = getattr(mod, "register", None)
        if callable(register):
            register(app)


_register_command_modules()

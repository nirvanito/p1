"""`lifecrm collect` — the imports the owner would run by hand, run by
themselves every morning (docs/collect.md).

Each source `[collect] sources` names is read where its connector finds it
with no path given — the backup app's OneDrive folder, the browser's own
history file, the newest health export in Downloads or OneDrive — and goes
through the very plan an `observe` or `health import` dry run shows. What
happens next is decided here, per source, and recorded (domain/collect.py):

  applied    a routine plan, written as `--apply` writes it: new and updated
             records, refreshed ones, the few deletions a mirroring source
             made (snapshot first, as observe takes one), the coverage a new
             file declares
  unchanged  nothing the database does not already hold; nothing written
  held       nothing written; the owner looks and runs it by hand:
               first_import  never imported from this source (and account)
                             before — the first import of every source is
                             the owner's, with its doc's first-real-import
                             checklist; a new phone or account starts over
               mass_removal  the removal observe would refuse
               invalid       records the reader produced but cannot validate
  missing    nothing found where the connector looks
  error      the reader failed on what it found

A conflict — a stored record the source now spells differently, with no
newer modification time — is never written by any import, so it holds
nothing back: the plan applies around it and the count is kept, as observe
reports it. The importer rule holds unattended too: nothing here writes
entries, measurements, mentions, aliases or entity links.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db as db_mod
from .config import Config
from .domain import collect as collect_dom
from .domain.util import DomainError, new_uuid, now_utc

SECTION = "collect"
HEALTH = "health"
# A connector's doc, where it is not docs/<name>.md (CLAUDE.md, Layout).
DOCS = {"ai_chat": "ai-chat.md", "ios_messages": "texts.md", "mac_messages": "texts.md",
        "ios_calls": "calls.md", "android_sms": "android-texts.md"}


# ---------------------------------------------------------------- settings

def collectable() -> list[str]:
    """Every source a run can take: health, and each connector that knows
    where its data lives (`default_target`). One that needs a path every
    time (an AI chat export) is not among them."""
    from .connectors import base as conn_base
    return [HEALTH] + sorted(n for n, c in conn_base.registry().items()
                             if callable(getattr(c, "default_target", None)))


def check_sources(names: list[str], where: str = "collect") -> list[str]:
    """`names` validated, duplicates dropped, order kept; one-line errors."""
    from .connectors import base as conn_base
    reg = conn_base.registry()
    out: list[str] = []
    for n in names:
        if n != HEALTH and n not in reg:
            raise DomainError(f"{where}: unknown source '{n}' — one of: "
                              f"{', '.join(collectable())}")
        if n != HEALTH and not callable(getattr(reg[n], "default_target", None)):
            raise DomainError(f"{where}: {n} needs the export's path every time, so it "
                              f"cannot be collected — import it by hand: python -m "
                              f"lifecrm observe {n} <export>")
        if n not in out:
            out.append(n)
    return out


def settings(cfg: Config) -> dict:
    """[collect] in config.toml: `sources`, the list a run takes when none
    is named (missing means none — the scheduled job then does nothing), and
    `health_dir`, a folder searched for the newest health export before the
    usual places (a phone's scheduled export often lands in a subfolder of
    the cloud drive, which `health import` alone never looks in)."""
    raw = (cfg.extra or {}).get(SECTION, {})
    if not isinstance(raw, dict):
        raise DomainError(f"[{SECTION}] must be a table with a sources list")
    unknown = sorted(set(raw) - {"sources", "health_dir"})
    if unknown:
        raise DomainError(f"[{SECTION}] has unknown setting(s) {', '.join(unknown)} — "
                          f"known: sources, health_dir")
    sources = raw.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) and s.strip()
                                                for s in sources):
        raise DomainError(f'[{SECTION}] sources must be a list of source names, e.g. '
                          f'["android_sms", "health"], got {sources!r}')
    health_dir = raw.get("health_dir")
    if health_dir is not None and (not isinstance(health_dir, str) or not health_dir.strip()):
        raise DomainError(f"[{SECTION}] health_dir must be a folder path in quotes, "
                          f"got {health_dir!r}")
    return {"sources": check_sources([s.strip() for s in sources],
                                     where=f"[{SECTION}] sources"),
            "health_dir": cfg.resolve(health_dir.strip()) if health_dir else None}


# ---------------------------------------------------------------- shared apply

def apply_observed(conn: sqlite3.Connection, cfg: Config, scan: dict, plan: dict, *,
                   allow_mass_removal: bool = False) -> tuple[dict, Path | None]:
    """observe --apply's write, shared with collect: refused on a mass
    removal unless allowed, a snapshot first when the plan removes anything,
    then the plan. Returns (apply_plan's summary, the snapshot or None). The
    caller owns the transaction."""
    from .domain import observations as obs
    refusal = obs.mass_removal(plan)
    if refusal and not allow_mass_removal:
        raise DomainError(refusal)
    snapshot = None
    if plan.get("removed"):
        if conn.in_transaction:
            # The backup API cannot copy a database its own connection is
            # writing: Python retries SQLITE_LOCKED forever, a silent hang.
            raise DomainError("a removal is snapshotted first, and this connection "
                              "has uncommitted writes — commit or roll back first")
        # Removal is the one write here that a re-import cannot always
        # undo, so it gets the migration treatment: a snapshot first. Not
        # named lifecrm-*: tiered retention keeps one of those per day, the
        # newest, so the next backup that day — taken after the removal —
        # would prune the only copy of what this removes. pre-removal-*
        # has its own retention (lifecrm/retention.py).
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        snapshot = db_mod.snapshot(conn, cfg.backups_dir / f"pre-removal-{ts}.db")
    summary = obs.apply_plan(conn, scan, plan, allow_mass_removal=allow_mass_removal,
                             snapshot=snapshot.name if snapshot else None)
    return summary, snapshot


# ---------------------------------------------------------------- one source

def _doc(name: str) -> str:
    return f"docs/{DOCS.get(name, name + '.md')}"


def _error_line(e: Exception) -> str:
    if isinstance(e, DomainError):
        return str(e)
    if isinstance(e, PermissionError):
        return (f"permission denied reading {e.filename or e} — on a Mac, grant your "
                f"terminal Full Disk Access (System Settings > Privacy & Security)")
    return f"{type(e).__name__}: {e}"


def _plan_counts(plan: dict) -> dict:
    counts = {"new": len(plan["new"]), "updated": len(plan["updates"]),
              "refreshed": len(plan["refreshed"]), "identical": plan["identical"],
              "conflicts": len(plan["conflicts"])}
    for key in ("invalid", "removed", "reappeared", "format_upgrades"):
        if plan.get(key):
            counts[key] = len(plan[key])
    return {k: v for k, v in counts.items() if v}


def _imported_before(conn: sqlite3.Connection, scan: dict) -> bool:
    """An applied import from this connector and account exists. Retracting
    every one of them counts as never: the owner undid it."""
    return conn.execute(
        "SELECT 1 FROM imports WHERE connector=? AND account IS ? AND status='applied' "
        "LIMIT 1", (scan["connector"], scan.get("account"))).fetchone() is not None


def _nothing_new(conn: sqlite3.Connection, scan: dict, plan: dict) -> bool:
    """Nothing to write. Coverage counts as something only from a file not
    applied before: re-reading yesterday's backup declares nothing new, while
    a new backup with no new texts still says today was quiet — a known zero."""
    if (plan["new"] or plan["updates"] or plan["refreshed"] or plan.get("removed")
            or plan.get("reappeared") or plan.get("format_upgrades")):
        return False
    if not scan.get("coverage"):
        return True
    if not scan.get("source_hash"):
        return False
    return conn.execute(
        "SELECT 1 FROM imports WHERE connector=? AND source_hash=? AND status='applied' "
        "LIMIT 1", (scan["connector"], scan["source_hash"])).fetchone() is not None


def _observed(conn: sqlite3.Connection, cfg: Config, name: str, *,
              dry_run: bool) -> dict:
    from .connectors import base as conn_base
    from .domain import observations as obs
    c = conn_base.get(name)
    try:
        target = c.default_target(cfg)
    except DomainError as e:
        return {"outcome": "missing", "reason": str(e)}
    scan = c.scan(target, cfg)
    plan = obs.build_plan(conn, scan)
    base = {"target": str(target), "counts": _plan_counts(plan)}
    look = f'python -m lifecrm observe {name} "{target}"'
    if not _imported_before(conn, scan):
        whose = f" for {scan['account']}" if scan.get("account") else ""
        return {**base, "outcome": "held", "hold": "first_import",
                "reason": f"first import from {name}{whose} — every source's first import "
                          f"is yours: {look} (a dry run), then add --apply; the checks "
                          f"are in {_doc(name)}"}
    if plan["invalid"]:
        return {**base, "outcome": "held", "hold": "invalid",
                "reason": f"{len(plan['invalid'])} record(s) the {name} reader cannot "
                          f"validate, first: {plan['invalid'][0]} — {look} shows them"}
    refusal = obs.mass_removal(plan)
    if refusal:
        return {**base, "outcome": "held", "hold": "mass_removal",
                "reason": f"{refusal} — {look} lists what would go"}
    if _nothing_new(conn, scan, plan):
        return {**base, "outcome": "unchanged"}
    if dry_run:
        return {**base, "outcome": "applied", "dry_run": True}
    summary, snapshot = apply_observed(conn, cfg, scan, plan)
    return {**base, "outcome": "applied", "import_uuid": summary["import_uuid"],
            "counts": {k: v for k, v in summary.items() if k != "import_uuid" and v},
            "snapshot": str(snapshot) if snapshot else None}


def _health_imported_before(conn: sqlite3.Connection, apple: bool) -> bool:
    """Active health records from this phone's kind of export exist (Apple
    Health ids carry the 'ah1:' prefix, Health Connect's are its own)."""
    from .apple_health import ID_PREFIX
    op = "LIKE" if apple else "NOT LIKE"
    return conn.execute(
        f"SELECT 1 FROM health_records WHERE status='active' AND external_id {op} ? "
        f"LIMIT 1", (ID_PREFIX + "%",)).fetchone() is not None


def _health(conn: sqlite3.Connection, cfg: Config, name: str, *,
            dry_run: bool) -> dict:
    from . import apple_health
    from . import health_import as hi
    first = settings(cfg)["health_dir"]
    search = hi.find_exports(([first] if first else []) + hi.search_dirs())
    if not search["found"]:
        return {"outcome": "missing", "reason": hi.not_found_message(search)
                .replace("python -m lifecrm health import <path>",
                         "python -m lifecrm health import <path>, or set [collect] "
                         "health_dir to the folder the phone exports to")}
    target = search["found"][0]
    reader = hi.reader_for(target)
    apple = reader is apple_health
    scan = reader.read_export(target, cfg)
    plan = reader.build_plan(conn, scan["records"])
    counts = {"new": len(plan["new"]), "updated": len(plan["updates"]),
              "refreshed": len(plan["refreshed"]), "identical": plan["identical"],
              "conflicts": len(plan["conflicts"]),
              "absent": sum((plan.get("absent") or {}).values())}
    base = {"target": str(target), "counts": {k: v for k, v in counts.items() if v}}
    if not _health_imported_before(conn, apple):
        label, doc = (("Apple Health", "apple-health.md") if apple
                      else ("Health Connect", "health-connect.md"))
        return {**base, "outcome": "held", "hold": "first_import",
                "reason": f"first {label} import — every source's first import is "
                          f'yours: python -m lifecrm health import "{target}" (a dry '
                          f"run), then add --apply; the checks are in docs/{doc}"}
    if not (plan["new"] or plan["updates"] or plan["refreshed"]):
        return {**base, "outcome": "unchanged"}
    if dry_run:
        return {**base, "outcome": "applied", "dry_run": True}
    summary = reader.apply_plan(conn, plan)
    return {**base, "outcome": "applied", "counts": {k: v for k, v in summary.items() if v}}


def collect_source(conn: sqlite3.Connection, cfg: Config, name: str, *,
                   run_uuid: str, dry_run: bool = False) -> dict:
    """One source, start to finish: decide, apply when routine, record. An
    applied import and the row that says so commit together; a failure rolls
    the source back and is recorded instead. Never raises for one source —
    the next one still runs."""
    started = now_utc()
    try:
        found = (_health if name == HEALTH else _observed)(conn, cfg, name,
                                                           dry_run=dry_run)
    except Exception as e:                # one source failing never stops the others
        conn.rollback()
        found = {"outcome": "error", "reason": _error_line(e)}
    out = {"source": name, "hold": None, "reason": None, "target": None,
           "import_uuid": None, "counts": {}, **found}
    if dry_run:
        conn.rollback()
        return out
    try:
        collect_dom.record(conn, run_uuid=run_uuid, source=name, outcome=out["outcome"],
                           hold=out["hold"], reason=out["reason"], target=out["target"],
                           import_uuid=out["import_uuid"], counts=out["counts"],
                           started_at=started, finished_at=now_utc())
        conn.commit()
    except (DomainError, sqlite3.Error) as e:
        conn.rollback()
        out = {**out, "outcome": "error", "hold": None, "import_uuid": None,
               "reason": f"not recorded, nothing written: {e}", "record_failed": True}
    return out


def run(conn: sqlite3.Connection, cfg: Config, sources: list[str] | None = None, *,
        dry_run: bool = False) -> dict:
    """Every source named (default: [collect] sources), in order. Each source
    commits or rolls back on its own, so the connection must arrive with
    nothing pending — a caller's half-done write is never committed here."""
    if conn.in_transaction:
        raise DomainError("collect needs a connection with no uncommitted writes")
    names = check_sources(sources) if sources else settings(cfg)["sources"]
    run_uuid = new_uuid()
    results = [collect_source(conn, cfg, n, run_uuid=run_uuid, dry_run=dry_run)
               for n in names]
    return {"run_uuid": run_uuid, "dry_run": dry_run, "sources": results}


def status(conn: sqlite3.Connection, cfg: Config) -> dict:
    """The newest outcome of every configured source (and of any other
    source ever collected), when it last applied, and when a run last
    started. Reads only."""
    configured = settings(cfg)["sources"]
    latest = collect_dom.latest(conn)
    applied = collect_dom.last_applied(conn)
    rows = []
    for name in configured + sorted(set(latest) - set(configured)):
        row = latest.get(name)
        rows.append({"source": name, "configured": name in configured,
                     "latest": row, "last_applied": applied.get(name)})
    return {"configured": configured, "last_run_at": collect_dom.last_run_at(conn),
            "sources": rows}

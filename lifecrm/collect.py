"""`lifecrm collect` — the imports the owner would run by hand, run by
themselves every morning (docs/collect.md).

Which sources: with no `[collect] sources` in config.toml, every source that
knows where its data lives on this computer (`default_target`, and health)
— the owner configures nothing. A source it cannot find is skipped without
a word; one found for the first time becomes a question ("new on this
computer: browser history, 12,034 records — import it?") that `collect
approve` answers yes and `collect decline` answers never. A `[collect]
sources` list, when set, names the sources instead and nothing else is
looked at.

Each source is read where its connector finds it with no path given — the
backup app's OneDrive folder, the browser's own history file, the newest
health export in Downloads or OneDrive — and goes through the very plan an
`observe` or `health import` dry run shows. Then, per source, recorded
(domain/collect.py):

  applied    a routine plan, written as `--apply` writes it: new and updated
             records, refreshed ones, the few deletions a mirroring source
             made (snapshot first, as observe takes one), the coverage a new
             file declares
  unchanged  nothing the database does not already hold; nothing written
  held       nothing written; it waits for the owner's yes:
               first_import  never imported from this source (and account)
                             before — the first import of every source is
                             the owner's (`collect approve` shows the dry
                             run and imports on yes); a new phone or account
                             starts over
               mass_removal  the removal observe would refuse
               invalid       records the reader produced but cannot validate
  missing    a source the owner named found nothing where it looks
  error      the reader failed on what it found

A conflict — a stored record the source now spells differently, with no
newer modification time — is never written by any import, so it holds
nothing back: the plan applies around it and the count is kept, as observe
reports it. The importer rule holds unattended too: nothing here writes
entries, measurements, mentions, aliases or entity links.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db as db_mod
from . import platforms
from .config import Config
from .domain import collect as collect_dom
from .domain import nudges as nudge_state
from .domain.util import DomainError, new_uuid, now_utc

SECTION = "collect"
HEALTH = "health"
# A connector's doc, where it is not docs/<name>.md (CLAUDE.md, Layout).
DOCS = {"ai_chat": "ai-chat.md", "ios_messages": "texts.md", "mac_messages": "texts.md",
        "ios_calls": "calls.md", "android_sms": "android-texts.md"}
# What each source is, in the words a question to the owner uses.
LABELS = {
    HEALTH: "health data", "android_sms": "texts and calls (SMS Backup & Restore)",
    "browser": "browser history", "calendar": "calendar events",
    "contacts": "contact cards", "email": "email", "finance": "bank and card transactions",
    "fitness": "workouts", "google_activity": "Google searches and YouTube history",
    "ios_calls": "iPhone calls", "ios_messages": "iPhone texts (from a backup)",
    "linkedin": "LinkedIn connections and messages", "location": "places visited",
    "mac_messages": "Messages on this Mac", "meta_messages": "Instagram and Messenger messages",
    "notes": "notes", "nutrition": "the food diary", "payments": "Venmo and PayPal payments",
    "photos": "photo dates and places", "reading": "Kindle highlights and Goodreads books",
    "rides": "Uber rides and Eats orders", "spotify": "Spotify listening",
    "telegram": "Telegram messages", "whatsapp": "WhatsApp chats",
    # the folders exports land in, when macOS keeps the job out of them
    "downloads": "the Downloads folder", "icloud_drive": "iCloud Drive",
}
# Where exports land. macOS guards both (TCC); a listing it refuses is an
# empty list to pathlib's glob, so a refusal is looked for here, directly.
DROP_FOLDERS = ("downloads", "icloud_drive")
# Sources whose files exist on any Mac whether or not the phone feeds them:
# their checklist steps show until they are collected.
ALWAYS_STEPS = {"ios_calls", "mac_messages"}
# The owner's "never" for a source: a dismissed nudge key, so `nudges
# restore` undoes it like any other dismissal (domain/nudges.py).
DECLINE_KEYS = ("declined", "first_import")


def label(name: str) -> str:
    return LABELS.get(name, name)


# ---------------------------------------------------------------- settings

def collectable() -> list[str]:
    """Every source a run can take: health, and each connector that knows
    where its data lives (`default_target`). One that needs a path every
    time (an AI chat export) is not among them."""
    from . import online
    from .connectors import base as conn_base
    fed = online.feeds()
    return [HEALTH] + sorted(n for n, c in conn_base.registry().items()
                             if callable(getattr(c, "default_target", None)) or n in fed)


def check_sources(names: list[str], where: str = "collect") -> list[str]:
    """`names` validated, duplicates dropped, order kept; one-line errors."""
    from . import online
    from .connectors import base as conn_base
    reg = conn_base.registry()
    fed = online.feeds()
    out: list[str] = []
    for n in names:
        if n != HEALTH and n not in reg:
            raise DomainError(f"{where}: unknown source '{n}' — one of: "
                              f"{', '.join(collectable())}")
        if n != HEALTH and n not in fed and not callable(
                getattr(reg[n], "default_target", None)):
            raise DomainError(f"{where}: {n} needs the export's path every time, so it "
                              f"cannot be collected — import it by hand: python -m "
                              f"lifecrm observe {n} <export>")
        if n not in out:
            out.append(n)
    return out


def settings(cfg: Config) -> dict:
    """[collect] in config.toml, all optional. `sources`: the list a run
    takes instead of finding them itself (None, the default, means find
    them). `health_dir`: a folder searched for the newest health export
    before the usual places."""
    raw = (cfg.extra or {}).get(SECTION, {})
    if not isinstance(raw, dict):
        raise DomainError(f"[{SECTION}] must be a table")
    unknown = sorted(set(raw) - {"sources", "health_dir"})
    if unknown:
        raise DomainError(f"[{SECTION}] has unknown setting(s) {', '.join(unknown)} — "
                          f"known: sources, health_dir")
    sources = raw.get("sources")
    if sources is not None and (not isinstance(sources, list) or not all(
            isinstance(s, str) and s.strip() for s in sources)):
        raise DomainError(f'[{SECTION}] sources must be a list of source names, e.g. '
                          f'["android_sms", "health"] — or leave it out and every '
                          f'source on this computer is found — got {sources!r}')
    health_dir = raw.get("health_dir")
    if health_dir is not None and (not isinstance(health_dir, str) or not health_dir.strip()):
        raise DomainError(f"[{SECTION}] health_dir must be a folder path in quotes, "
                          f"got {health_dir!r}")
    return {"sources": (None if sources is None else
                        check_sources([s.strip() for s in sources],
                                      where=f"[{SECTION}] sources")),
            "health_dir": cfg.resolve(health_dir.strip()) if health_dir else None}


def declined(conn: sqlite3.Connection) -> set[str]:
    """Sources the owner said never to: `collect decline`, or the first-import
    question dismissed in /morning."""
    out = set()
    for key, row in nudge_state.state_for(conn).items():
        parts = key.split(":")
        if (len(parts) == 3 and parts[0] == "collect" and parts[2] in DECLINE_KEYS
                and row["state"] == "dismissed"):
            out.add(parts[1])
    return out


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


# ---------------------------------------------------------------- reading a source

def _doc(name: str) -> str:
    return f"docs/{DOCS.get(name, name + '.md')}"


def has_checklist(doc: str) -> bool:
    """The doc ends in a first-real-import checklist (a heading saying so):
    worth naming in a preview only when it is there."""
    import re
    path = Path(__file__).resolve().parent.parent / doc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"^#+ .*checklist", text, re.I | re.M) is not None


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
    if plan.get("absent"):                         # Apple Health only
        counts["absent"] = sum(plan["absent"].values())
    return {k: v for k, v in counts.items() if v}


def _span(records: list[dict]) -> tuple[str, str] | None:
    days = sorted(r["journal_date"] for r in records if r.get("journal_date"))
    return (days[0], days[-1]) if days else None


def _imported_before(conn: sqlite3.Connection, scan: dict) -> bool:
    """An applied import from this connector and account exists. Retracting
    every one of them counts as never: the owner undid it."""
    return conn.execute(
        "SELECT 1 FROM imports WHERE connector=? AND account IS ? AND status='applied' "
        "LIMIT 1", (scan["connector"], scan.get("account"))).fetchone() is not None


def in_use(conn: sqlite3.Connection, name: str) -> bool:
    """The source was imported before, from any account: health records of
    any kind, or an applied import of the connector."""
    if name == HEALTH:
        return conn.execute("SELECT 1 FROM health_records WHERE status='active' "
                            "LIMIT 1").fetchone() is not None
    return conn.execute("SELECT 1 FROM imports WHERE connector=? AND status='applied' "
                        "LIMIT 1", (name,)).fetchone() is not None


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


class NotFound(DomainError):
    """Nothing where the source looks: a question for the owner only when
    they named the source."""


class Blocked(DomainError):
    """The source is there, but macOS privacy (TCC) refuses the read until
    the owner grants Full Disk Access — never 'not found', which would hide
    the one thing they need to do."""


FULL_DISK_ACCESS_STEPS = [
    "System Settings › Privacy & Security › Full Disk Access › + › your terminal "
    "(Terminal, iTerm, or the app Claude Code runs in), then quit and reopen it.",
    "For the 06:30 job too: add /bin/bash (in the file picker press ⌘⇧G, type "
    "/bin/bash) and the venv's real Python — docs/mac.md, section 5."]


def refused_folders() -> dict[str, Path]:
    """{key: folder} for each drop folder (Downloads, iCloud Drive) this
    process may not list. Looks only."""
    places = {"downloads": platforms.home() / "Downloads",
              "icloud_drive": platforms.icloud_drive()}
    out = {}
    for key in DROP_FOLDERS:
        path = places[key]
        try:
            if path.is_dir():
                os.listdir(path)
        except PermissionError:
            out[key] = path
        except OSError:
            continue
    return out


def _folder_refusal(path: Path) -> str:
    return (f"macOS did not let this look in {path} — exports saved there are not "
            f"found until it may: give Full Disk Access (docs/mac.md, section 5)")


def blocked(e: BaseException | None) -> bool:
    """macOS refused a read: a PermissionError anywhere in the chain, or a
    connector's own one-line Full Disk Access error (every Mac connector
    words its denial that way)."""
    seen: set[int] = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, PermissionError) or "Full Disk Access" in str(e):
            return True
        e = e.__cause__ or e.__context__
    return False


def _health_dirs(cfg: Config) -> list[Path]:
    """Where a health export is looked for: [collect] health_dir, the usual
    places (`health import`'s), and one folder down in the cloud drives — a
    phone's scheduled export usually lands in a folder of its own."""
    from . import health_import as hi
    first = settings(cfg)["health_dir"]
    dirs = ([first] if first else []) + hi.search_dirs()
    for root in platforms.onedrive_candidates():
        try:
            dirs += sorted(p for p in root.iterdir() if p.is_dir()
                           and not p.name.startswith("."))
        except OSError:
            continue
    return list(dict.fromkeys(dirs))


def _read(conn: sqlite3.Connection, cfg: Config, name: str) -> dict:
    """Find the source, read it, plan it: everything a decision needs.
    NotFound when nothing is where it looks."""
    if name == HEALTH:
        from . import apple_health
        from . import health_import as hi
        search = hi.find_exports(_health_dirs(cfg))
        if not search["found"]:
            raise NotFound(hi.not_found_message(search).replace(
                "python -m lifecrm health import <path>",
                "python -m lifecrm health import <path>, or set [collect] health_dir "
                "to the folder the phone exports to"))
        target = search["found"][0]
        reader = hi.reader_for(target)
        scan = reader.read_export(target, cfg)
        plan = reader.build_plan(conn, scan["records"])
        apple = reader is apple_health
        return {"kind": "health", "target": target, "scan": scan, "plan": plan,
                "reader": reader, "apple": apple,
                "label": "health data (Apple Health)" if apple
                else "health data (Health Connect)",
                "doc": "docs/apple-health.md" if apple else "docs/health-connect.md",
                "look": f'python -m lifecrm health import "{target}"'}
    from . import online
    from .connectors import base as conn_base
    from .domain import observations as obs
    c = conn_base.get(name)
    service = online.for_connector(cfg, name)
    try:
        # A connected online service: today's data fetched first, then read
        # like any export (lifecrm/online). A failed fetch is an error, never
        # "not found": the owner connected it.
        target = (online.fetch(cfg, service) if service is not None
                  else c.default_target(cfg))
    except (DomainError, PermissionError) as e:
        if service is not None:
            raise
        if blocked(e):
            raise Blocked(_error_line(e)) from e
        if isinstance(e, conn_base.Ambiguous):
            raise                         # several candidates: a question, not absence
        raise NotFound(str(e)) from e
    scan = c.scan(target, cfg)
    return {"kind": "observed", "target": target, "scan": scan,
            "plan": obs.build_plan(conn, scan), "connector": c, "label": label(name),
            "doc": _doc(name), "look": f'python -m lifecrm observe {name} "{target}"'}


def _first_time(conn: sqlite3.Connection, src: dict) -> bool:
    if src["kind"] == "health":
        from .apple_health import ID_PREFIX
        op = "LIKE" if src["apple"] else "NOT LIKE"
        return conn.execute(
            f"SELECT 1 FROM health_records WHERE status='active' AND external_id {op} ? "
            f"LIMIT 1", (ID_PREFIX + "%",)).fetchone() is None
    return not _imported_before(conn, src["scan"])


def _question(name: str, src: dict) -> str:
    """The one line a new source is offered in: what, how much, which days."""
    n = len(src["plan"]["new"])
    span = _span(src["plan"]["new"])
    whose = (f" for {src['scan']['account']}" if src["kind"] == "observed"
             and src["scan"].get("account") else "")
    days = f" from {span[0]} to {span[1]}" if span else ""
    return (f"new on this computer: {src['label']}{whose}, {n:,} record(s){days}, in "
            f"{src['target']} — yes imports them: python -m lifecrm collect approve "
            f"{name}; never: python -m lifecrm collect decline {name}; what to check "
            f"first: {src['doc']}")


def _decide(conn: sqlite3.Connection, cfg: Config, name: str, *, dry_run: bool,
            auto: bool, refused: dict[str, Path] | None = None) -> dict:
    """One source's outcome, writing only an applied plan (uncommitted). A
    found-for-the-first-time source the reader cannot read, or holding
    nothing, is `skipped` when it was found rather than named: there is
    nothing to ask. Several candidates (two phones' backups) are an error
    that says which, whichever way the source came."""
    from .connectors import base as conn_base
    using = in_use(conn, name)

    def refusal(e: BaseException) -> dict:
        line = _error_line(e)
        if auto and any(str(p) in line for p in (refused or {}).values()):
            # the folder's own row already says it, once for every source
            return {"outcome": "skipped", "reason": line, "skip": "hidden"}
        return {"outcome": "error", "reason": line, "blocked": True}
    try:
        src = _read(conn, cfg, name)
    except Blocked as e:
        return refusal(e)
    except NotFound as e:
        hidden = [p for p in (refused or {}).values() if str(p) in str(e)]
        if hidden and not auto:           # named, and it looked where macOS said no
            return {"outcome": "error", "reason": _folder_refusal(hidden[0]),
                    "blocked": True}
        return {"outcome": "skipped" if auto else "missing", "reason": str(e),
                "skip": "not found"}
    except conn_base.Ambiguous as e:
        return {"outcome": "error",
                "reason": f"{e} — set its path in [connectors.{name}] or import the one "
                          f"you mean by hand; never: python -m lifecrm collect decline "
                          f"{name}"}
    except Exception as e:
        if blocked(e):                    # refused while reading, not while looking
            return refusal(e)
        from . import online
        if auto and not using and online.for_connector(cfg, name) is None:
            return {"outcome": "skipped", "reason": _error_line(e), "skip": "unreadable"}
        raise
    plan = src["plan"]
    base = {"target": str(src["target"]), "counts": _plan_counts(plan)}
    if _first_time(conn, src):
        if auto and not plan["new"]:
            return {**base, "outcome": "skipped", "reason": "holds no records",
                    "skip": "empty"}
        return {**base, "outcome": "held", "hold": "first_import",
                "reason": _question(name, src)}
    if src["kind"] == "health":
        if not (plan["new"] or plan["updates"] or plan["refreshed"]):
            return {**base, "outcome": "unchanged"}
        if dry_run:
            return {**base, "outcome": "applied", "dry_run": True}
        summary = src["reader"].apply_plan(conn, plan)
        return {**base, "outcome": "applied",
                "counts": {k: v for k, v in summary.items() if v}}
    from .domain import observations as obs
    if plan["invalid"]:
        return {**base, "outcome": "held", "hold": "invalid",
                "reason": f"{len(plan['invalid'])} record(s) the {name} reader cannot "
                          f"validate, first: {plan['invalid'][0]} — {src['look']} "
                          f"shows them"}
    refusal = obs.mass_removal(plan)
    if refusal:
        return {**base, "outcome": "held", "hold": "mass_removal",
                "reason": f"{refusal} — to see what would go, and import on yes: "
                          f"python -m lifecrm collect approve {name}"}
    if _nothing_new(conn, src["scan"], plan):
        return {**base, "outcome": "unchanged"}
    if dry_run:
        return {**base, "outcome": "applied", "dry_run": True}
    summary, snapshot = apply_observed(conn, cfg, src["scan"], plan)
    return {**base, "outcome": "applied", "import_uuid": summary["import_uuid"],
            "counts": {k: v for k, v in summary.items() if k != "import_uuid" and v},
            "snapshot": str(snapshot) if snapshot else None}


def _record(conn: sqlite3.Connection, out: dict, run_uuid: str, started: str,
            actor: str = "system") -> dict:
    """The outcome's row, committed with whatever the source applied."""
    try:
        collect_dom.record(conn, run_uuid=run_uuid, source=out["source"],
                           outcome=out["outcome"], hold=out["hold"], reason=out["reason"],
                           target=out["target"], import_uuid=out["import_uuid"],
                           counts=out["counts"], started_at=started,
                           finished_at=now_utc(), actor=actor)
        conn.commit()
    except (DomainError, sqlite3.Error) as e:
        conn.rollback()
        out = {**out, "outcome": "error", "hold": None, "import_uuid": None,
               "reason": f"not recorded, nothing written: {e}", "record_failed": True}
    return out


def collect_source(conn: sqlite3.Connection, cfg: Config, name: str, *,
                   run_uuid: str, dry_run: bool = False, auto: bool = False,
                   refused: dict[str, Path] | None = None) -> dict:
    """One source, start to finish: decide, apply when routine, record. An
    applied import and the row that says so commit together; a failure rolls
    the source back and is recorded instead; a skipped source leaves no row.
    Never raises for one source — the next one still runs."""
    started = now_utc()
    try:
        found = _decide(conn, cfg, name, dry_run=dry_run, auto=auto, refused=refused)
    except Exception as e:                # one source failing never stops the others
        conn.rollback()
        found = {"outcome": "error", "reason": _error_line(e)}
    out = {"source": name, "hold": None, "reason": None, "target": None,
           "import_uuid": None, "counts": {}, **found}
    if dry_run or out["outcome"] == "skipped":
        conn.rollback()
        return out
    out = _record(conn, out, run_uuid, started)
    if out["outcome"] in ("applied", "unchanged") and out.get("target"):
        _settle(cfg, name, out["target"])
    return out


def _settle(cfg: Config, name: str, target: str) -> None:
    """After a committed import: an online service that fetches only what is
    new (mail) files away what is now in the database, so tomorrow's read is
    tomorrow's mail. Before the yes it stays put: the first import reads it."""
    from . import online
    try:
        online.settle(cfg, name, Path(target))
    except (DomainError, OSError):
        pass                 # left pending: re-read tomorrow, deduped, harmless


def run(conn: sqlite3.Connection, cfg: Config, sources: list[str] | None = None, *,
        dry_run: bool = False) -> dict:
    """The sources named here, else [collect] sources, else every source on
    this computer — less the ones the owner declined. Each source commits or
    rolls back on its own, so the connection must arrive with nothing
    pending: a caller's half-done write is never committed here."""
    if conn.in_transaction:
        raise DomainError("collect needs a connection with no uncommitted writes")
    configured = settings(cfg)["sources"]
    names = check_sources(sources) if sources else configured
    auto = names is None
    if auto:
        names = collectable()
    no = declined(conn)
    refused = refused_folders()
    run_uuid = new_uuid()
    results = []
    for n in names:
        if n in no:
            results.append({"source": n, "outcome": "skipped", "skip": "declined",
                            "reason": "you declined it", "hold": None, "target": None,
                            "import_uuid": None, "counts": {}})
            continue
        results.append(collect_source(conn, cfg, n, run_uuid=run_uuid, dry_run=dry_run,
                                      auto=auto, refused=refused))
    if auto:
        results += _drop_folders(conn, {k: p for k, p in refused.items() if k not in no},
                                 run_uuid, dry_run)
    return {"run_uuid": run_uuid, "dry_run": dry_run, "auto": auto, "sources": results}


def _drop_folders(conn: sqlite3.Connection, refused: dict[str, Path], run_uuid: str,
                  dry_run: bool) -> list[dict]:
    """A drop folder macOS keeps the job out of is said once, as a row of its
    own (one thing to fix, whichever sources would have looked there); the
    morning it may look again, one row says so and the item goes."""
    latest = collect_dom.latest(conn)
    out = []
    for key in DROP_FOLDERS:
        was = latest.get(key)
        if key in refused:
            row = {"source": key, "outcome": "error", "hold": None, "blocked": True,
                   "reason": _folder_refusal(refused[key]),
                   "target": str(refused[key]), "import_uuid": None, "counts": {}}
        elif was is not None and was["outcome"] == "error":
            row = {"source": key, "outcome": "unchanged", "hold": None, "reason": None,
                   "target": was["target"], "import_uuid": None, "counts": {}}
        else:
            continue
        out.append(row if dry_run else _record(conn, row, run_uuid, now_utc()))
    return out


# ---------------------------------------------------------------- the owner's answers

def _forget_no(conn: sqlite3.Connection, name: str) -> None:
    states = nudge_state.state_for(conn)
    for why in DECLINE_KEYS:
        key = f"collect:{name}:{why}"
        if key in states:
            nudge_state.restore(conn, key)


def approve(conn: sqlite3.Connection, cfg: Config, name: str, *,
            apply: bool = False) -> dict:
    """The owner's yes. Without `apply`: what importing the source now would
    write — the connector's own dry-run report and the one-line question.
    With it: that import, a held removal included (the yes is what it
    waited for), recorded like a run. Records the reader cannot validate are
    never imported: they need a fix, not a yes."""
    if conn.in_transaction:
        raise DomainError("collect needs a connection with no uncommitted writes")
    check_sources([name], where="collect approve")
    try:
        src = _read(conn, cfg, name)
    except NotFound as e:
        raise DomainError(f"nothing to approve for {name}: {e} — python -m lifecrm "
                          f"collect setup shows how to get it onto this computer") from e
    plan, scan = src["plan"], src["scan"]
    report = (src["reader"].report_lines(scan, plan) if src["kind"] == "health"
              else src["connector"].report_lines(scan, plan))
    out = {"source": name, "label": src["label"], "target": str(src["target"]),
           "counts": _plan_counts(plan), "span": _span(plan["new"]),
           "first_import": _first_time(conn, src), "doc": src["doc"],
           "checklist": has_checklist(src["doc"]),
           "report": report, "applied": False}
    if src["kind"] == "observed":
        from .domain import observations as obs
        out["mass_removal"] = obs.mass_removal(plan)
        if plan["invalid"]:
            raise DomainError(f"{len(plan['invalid'])} record(s) the {name} reader cannot "
                              f"validate, first: {plan['invalid'][0]} — this needs a fix "
                              f"to the reader, not a yes")
        nothing = _nothing_new(conn, scan, plan) and not out["first_import"]
    else:
        nothing = not (plan["new"] or plan["updates"] or plan["refreshed"])
    out["nothing_new"] = nothing
    if not apply or nothing:
        conn.rollback()
        return out
    started = now_utc()
    if src["kind"] == "observed":
        summary, snapshot = apply_observed(conn, cfg, scan, plan, allow_mass_removal=True)
        out.update(import_uuid=summary["import_uuid"],
                   snapshot=str(snapshot) if snapshot else None)
        counts = {k: v for k, v in summary.items() if k != "import_uuid" and v}
    else:
        counts = {k: v for k, v in src["reader"].apply_plan(conn, plan).items() if v}
    _forget_no(conn, name)
    row = _record(conn, {"source": name, "outcome": "applied", "hold": None,
                         "reason": None, "target": str(src["target"]),
                         "import_uuid": out.get("import_uuid"), "counts": counts},
                  new_uuid(), started, actor="user")
    if row.get("record_failed"):
        raise DomainError(row["reason"])
    _settle(cfg, name, str(src["target"]))
    out.update(applied=True, applied_counts=counts)
    return out


def decline(conn: sqlite3.Connection, name: str) -> dict:
    """The owner's never: the source is not looked at again (`collect
    approve` or `nudges restore collect:<source>:declined` take it back). A
    drop folder (downloads, icloud_drive) can be declined too: the job stops
    saying macOS keeps it out."""
    if name not in DROP_FOLDERS:
        check_sources([name], where="collect decline")
    row = nudge_state.dismiss(conn, f"collect:{name}:declined",
                              note="never collect this source")
    conn.commit()
    return {"source": name, "key": row["nudge_key"]}


# ---------------------------------------------------------------- phone setup

# What the phone (or this computer) does so each source arrives by itself,
# per setup; the steps are the setup guides' own (docs/android.md,
# docs/windows-iphone.md, docs/mac.md), "(verify)" where those say it.
PHONE = {
    "android": ("Windows + Android", "docs/windows-android.md", [
        ("android_sms", "Texts and calls", [
            "Install SMS Backup & Restore (SyncTech) from the Play Store.",
            "Set up a backup: tick Messages and Call logs, and back up all "
            "conversations, not selected ones.",
            "Where: OneDrive — sign in, and keep the folder it offers (verify the name).",
            "Schedule recurring backups: daily, at night."]),
        (HEALTH, "Health (Samsung Health through Health Connect)", [
            "Samsung Health › ≡ › Settings › Health Connect: let it write sleep, steps, "
            "weight, exercise, heart rate and nutrition (verify the menu path).",
            "Health Connect › App permissions: only one app writing each metric.",
            "Health Connect › Export / Scheduled export: daily, to OneDrive (verify)."]),
        ("photos", "Photo dates and places (never the pictures)", [
            "Gallery › ≡ › Settings › Sync with OneDrive (verify), or the OneDrive "
            "app's Camera upload."]),
        ("browser", "Browser history", [
            "Nothing on this computer: Chrome's own history is read.",
            "On the phone: sign in to Chrome with the same Google account and turn "
            "on Sync, so the phone's browsing reaches this computer's Chrome."]),
    ]),
    "iphone-windows": ("Windows + iPhone", "docs/windows-iphone.md", [
        ("ios_messages", "Texts (from the iPhone's backup)", [
            "Install Apple Devices from the Microsoft Store and connect the iPhone "
            "with its cable.",
            "Under Backups: Back up all of the data on your iPhone to this computer, "
            "with Encrypt local backup unticked, then Back Up Now.",
            "Each later backup (the cable, or Wi-Fi while it charges if Apple "
            "Devices does that (verify)) is collected the next morning."]),
        (HEALTH, "Health (Apple Health)", [
            "Health › your picture › Export All Health Data › Save to Files › OneDrive "
            "(top level).",
            "The iPhone has no scheduled export: repeat it when you want newer data "
            "in; the next morning takes the newest file."]),
        ("photos", "Photo dates and places (never the pictures)", [
            "The OneDrive app on the iPhone › Camera upload, so photos land in "
            "OneDrive › Pictures › Camera Roll (verify)."]),
        ("browser", "Browser history", [
            "Nothing on this computer: the browser's own history is read."]),
    ]),
    "iphone-mac": ("Mac + iPhone", "docs/mac.md", [
        ("mac_messages", "Messages", [
            "Messages in iCloud on, on the iPhone and on this Mac, so the Mac holds "
            "the whole history.",
            "Give your terminal Full Disk Access: System Settings › Privacy & "
            "Security › Full Disk Access (docs/mac.md, section 5)."]),
        ("ios_calls", "Calls", [
            "FaceTime on this Mac › Settings › Calls from iPhone.",
            "iPhone › Settings › Apps › Phone › Calls on Other Devices, with this "
            "Mac allowed."]),
        (HEALTH, "Health (Apple Health)", [
            "Health › your picture › Export All Health Data › AirDrop to this Mac "
            "(it lands in Downloads).",
            "The iPhone has no scheduled export: repeat it when you want newer data "
            "in; the next morning takes the newest file."]),
        ("photos", "Photo dates and places (never the pictures)", [
            "iCloud Photos on in Photos › Settings on this Mac."]),
        ("contacts", "Contacts", ["iCloud Contacts on, on this Mac."]),
        ("notes", "Notes", ["iCloud Notes on, on this Mac."]),
        ("browser", "Browser history", [
            "Nothing on this Mac: Safari's or Chrome's own history is read."]),
    ]),
}


def guess_phone() -> str:
    """'iphone-mac' on a Mac; on Windows 'iphone-windows' when Apple's backup
    folder exists, else 'android'."""
    if platforms.is_mac():
        return "iphone-mac"
    for folder in platforms.mobilesync_backup_dirs():
        try:
            if folder.is_dir():
                return "iphone-windows"
        except OSError:
            continue
    return "android"


def _found(cfg: Config, name: str) -> str:
    """'found', 'missing', or 'blocked' (macOS refuses the look). Looks only:
    nothing is read."""
    try:
        if name == HEALTH:
            from . import health_import as hi
            if hi.find_exports(_health_dirs(cfg))["found"]:
                return "found"
            # an export in a folder macOS hides is an empty glob, not an error
            return "blocked" if refused_folders() else "missing"
        from .connectors import base as conn_base
        conn_base.get(name).default_target(cfg)
        return "found"
    except (DomainError, OSError) as e:
        return "blocked" if blocked(e) else "missing"


def setup_checklist(conn: sqlite3.Connection, cfg: Config,
                    phone: str | None = None) -> dict:
    """Per phone source: collecting (imported before), waiting (on this
    computer, never imported: `collect approve`), declined, or to do (not on
    this computer yet: the steps). Reads only."""
    phone = phone or guess_phone()
    if phone not in PHONE:
        raise DomainError(f"phone must be one of {', '.join(PHONE)}, got {phone!r}")
    title, guide, sources = PHONE[phone]
    no = declined(conn)
    configured = settings(cfg)["sources"]
    items = []
    for name, what, steps in sources:
        found = None if name in no else _found(cfg, name)
        if name in no:
            state = "declined"
        elif found == "blocked":
            state, steps = "blocked", FULL_DISK_ACCESS_STEPS
        elif in_use(conn, name):
            state = "collecting"
        elif found == "found":
            state = "waiting"
        else:
            state = "to do"
        if configured is not None and name not in configured and state != "declined":
            state += " (not in [collect] sources)"
        shown = state.startswith(("to do", "blocked")) or (
            state.startswith("waiting") and name in ALWAYS_STEPS)
        items.append({"source": name, "what": what, "state": state,
                      "steps": steps if shown else []})
    from . import online
    services = [{**s, "steps": [] if s["connected"] else s["steps"]}
                for s in online.status(cfg)]
    return {"phone": phone, "title": title, "guide": guide, "items": items,
            "online": services}


# ---------------------------------------------------------------- status

def status(conn: sqlite3.Connection, cfg: Config) -> dict:
    """The newest outcome of every source collected (or named in [collect]
    sources), when it last applied, when a run last started, and what the
    owner declined. Reads only."""
    configured = settings(cfg)["sources"]
    latest = collect_dom.latest(conn)
    applied = collect_dom.last_applied(conn)
    no = declined(conn)
    names = (configured or []) + sorted(set(latest) - set(configured or []))
    rows = []
    for name in names:
        rows.append({"source": name, "configured": configured is None or name in configured,
                     "declined": name in no, "latest": latest.get(name),
                     "last_applied": applied.get(name)})
    return {"configured": configured, "auto": configured is None,
            "last_run_at": collect_dom.last_run_at(conn), "declined": sorted(no),
            "sources": rows}

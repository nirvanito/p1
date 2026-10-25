"""`lifecrm collect` — the daily imports, run by themselves (docs/collect.md).

The decisions live in lifecrm/collect.py; this module owns the flags and the
report. scripts/collect.* runs it every morning with --if-configured, so a
machine with no [collect] sources logs one line and exits 0. The exit code
is 1 only when a source failed: a held source is a decision waiting for the
owner, not a failure of the job.
"""

from __future__ import annotations

import sqlite3

import typer

# Counts in the order a report reads them: (key, label).
_COUNT_LABELS = (("added", "added"), ("new", "new"), ("updated", "updated"),
                 ("refreshed", "refreshed"), ("removed", "removed"),
                 ("retracted_cited", "hidden instead (cited)"),
                 ("reappeared", "back in the source"),
                 ("format_upgraded", "format upgraded"),
                 ("format_upgrades", "format upgrades"),
                 ("coverage", "coverage declared"),
                 ("conflicts", "conflict(s) kept existing"), ("invalid", "invalid"),
                 ("absent", "stored but not in this export"))


def register(app: typer.Typer) -> None:
    app.command("collect")(collect)


def counts_text(counts: dict) -> str:
    parts = [f"{counts[k]} {label}" for k, label in _COUNT_LABELS if counts.get(k)]
    return ", ".join(parts) or "nothing new"


def _line(r: dict, dry_run: bool) -> str:
    outcome = "would apply" if dry_run and r["outcome"] == "applied" else r["outcome"]
    if r["outcome"] in ("applied", "unchanged"):
        what = counts_text(r["counts"])
        if r.get("snapshot"):
            what += f" (snapshot before removing: {r['snapshot']})"
        if r.get("import_uuid"):
            what += f" · import {r['import_uuid'][:8]}"
        if r.get("target"):
            what += f" · {r['target']}"
    else:
        what = r["reason"]
    return f"  {r['source']:<15} {outcome:<11} {what}"


def collect(sources: list[str] = typer.Argument(
                None, metavar="[SOURCE]...",
                help="Sources to collect now (default: [collect] sources in config.toml)"),
            dry_run: bool = typer.Option(False, "--dry-run",
                                         help="Decide every source, write nothing"),
            show_status: bool = typer.Option(False, "--status",
                                             help="Each source's last outcome (read-only)"),
            if_configured: bool = typer.Option(
                False, "--if-configured",
                help="Exit 0 with one line when [collect] names no sources (for the "
                     "scheduled job)"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Run the imports you would run by hand: routine changes are applied,
    anything unusual — a source's first import, a large removal, records the
    reader cannot validate — is held for you (docs/collect.md)."""
    from .. import collect as mod
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..domain.util import DomainError

    cfg = _cfg()
    if show_status:
        conn = _conn(cfg, readonly=True)
        try:
            report = mod.status(conn, cfg)
        except DomainError as e:
            _fail(str(e))
        except sqlite3.OperationalError as e:
            _fail(f"cannot read the collect history ({e}) — run: python -m lifecrm migrate")
        finally:
            conn.close()
        _echo_status(report, as_json)
        return

    try:
        names = mod.check_sources(sources) if sources else mod.settings(cfg)["sources"]
    except DomainError as e:
        _fail(str(e))
    if not names:
        if if_configured:
            msg = ("collect: no [collect] sources in config.toml — nothing collected "
                   "(docs/collect.md)")
            _echo_json({"skipped": True, "reason": msg}) if as_json else typer.echo(msg)
            return
        _fail("nothing to collect — name the sources (python -m lifecrm collect "
              "android_sms health) or list them in config.toml: [collect] sources = "
              '["android_sms", "health"] (docs/collect.md)')
    conn = _conn(cfg)
    try:
        result = mod.run(conn, cfg, names, dry_run=dry_run)
    except DomainError as e:
        _fail(str(e))
    finally:
        conn.close()

    failed = [r for r in result["sources"] if r["outcome"] == "error"]
    if as_json:
        _echo_json(result)
    else:
        n = len(result["sources"])
        typer.echo(f"dry run — nothing written; {n} source(s):" if dry_run else
                   f"collected {n} source(s) — run {result['run_uuid'][:8]}")
        for r in result["sources"]:
            typer.echo(_line(r, dry_run))
        held = [r["source"] for r in result["sources"]
                if r["outcome"] in ("held", "missing")]
        if held and not dry_run:
            typer.echo(f"waiting for you: {', '.join(held)} — in /morning and doctor "
                       f"until a later run finds them routine (collect --status)")
    if failed:
        raise typer.Exit(1)


def _echo_status(report: dict, as_json: bool) -> None:
    from ..cli import _echo_json
    if as_json:
        _echo_json(report)
        return
    last = report["last_run_at"]
    typer.echo(f"collect: {len(report['configured'])} source(s) in [collect] sources; "
               + (f"last run {last[:16].replace('T', ' ')} UTC" if last else "never run"))
    if not report["sources"]:
        typer.echo('  none — add to config.toml: [collect] sources = ["android_sms", '
                   '"health"] (docs/collect.md)')
    for s in report["sources"]:
        row = s["latest"]
        mark = "" if s["configured"] else " (no longer in [collect] sources)"
        if row is None:
            typer.echo(f"  {s['source']:<15} never collected{mark}")
            continue
        when = row["started_at"][:16].replace("T", " ")
        what = (counts_text(row["counts"]) if row["outcome"] in ("applied", "unchanged")
                else row["reason"])
        applied = (f" · last applied {s['last_applied'][:10]}"
                   if s["last_applied"] and row["outcome"] != "applied" else "")
        typer.echo(f"  {s['source']:<15} {row['outcome']:<10} {when}  {what}{applied}{mark}")

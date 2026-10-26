"""`lifecrm collect` — the daily imports, run by themselves (docs/collect.md).

    lifecrm collect                    every source on this computer (or [collect] sources)
    lifecrm collect run SOURCE...      just these, now
    lifecrm collect approve SOURCE     yes: shows what would come in, imports on yes (--yes)
    lifecrm collect decline SOURCE     never: the job stops looking at it
    lifecrm collect status             each source's last outcome
    lifecrm collect setup              the phone's side, as a checklist

The decisions live in lifecrm/collect.py; this module owns the flags and the
report. scripts/collect.* runs the plain form every morning with
--if-configured. The exit code is 1 only when a source failed: a source
waiting for the owner's yes is a question, not a failure of the job.
"""

from __future__ import annotations

import sqlite3
import sys

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
_SETUP_MARKS = {"collecting": "done ", "waiting": "ready", "to do": "to do",
                "declined": "  no ", "blocked": "  !  "}

collect_app = typer.Typer(
    help="The daily imports: every source on this computer, routine changes applied, "
         "anything new or unusual waiting for your yes (docs/collect.md).",
    invoke_without_command=True)


def register(app: typer.Typer) -> None:
    app.add_typer(collect_app, name="collect")


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


def _open(readonly: bool = False):
    from ..cli import _cfg, _conn
    cfg = _cfg()
    return cfg, _conn(cfg, readonly=readonly)


@collect_app.callback()
def collect(ctx: typer.Context,
            dry_run: bool = typer.Option(False, "--dry-run",
                                         help="Decide every source, write nothing"),
            show_status: bool = typer.Option(False, "--status",
                                             help="Each source's last outcome (read-only)"),
            if_configured: bool = typer.Option(
                False, "--if-configured",
                help="Exit 0 with one line when there is nothing to collect (for the "
                     "scheduled job)"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Run the imports you would run by hand, for every source this computer
    has (or the ones [collect] sources names): routine changes are applied; a
    new source, a large removal or records the reader cannot validate wait
    for your yes (docs/collect.md)."""
    if ctx.invoked_subcommand is not None:
        return
    if show_status:
        _status(as_json)
        return
    _run(None, dry_run=dry_run, if_configured=if_configured, as_json=as_json)


@collect_app.command("run")
def run_cmd(sources: list[str] = typer.Argument(..., metavar="SOURCE...",
                                                help="Sources to collect now"),
            dry_run: bool = typer.Option(False, "--dry-run",
                                         help="Decide every source, write nothing"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Collect the named sources now, by the same rules as the daily job."""
    _run(sources, dry_run=dry_run, if_configured=False, as_json=as_json)


def _run(sources: list[str] | None, *, dry_run: bool, if_configured: bool,
         as_json: bool) -> None:
    from .. import collect as mod
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..domain.util import DomainError

    cfg = _cfg()
    try:
        names = mod.check_sources(sources) if sources else mod.settings(cfg)["sources"]
    except DomainError as e:
        _fail(str(e))
    if names == []:
        msg = ("collect: [collect] sources is an empty list in config.toml — nothing "
               "collected (leave it out and every source on this computer is found)")
        if if_configured:
            _echo_json({"skipped": True, "reason": msg}) if as_json else typer.echo(msg)
            return
        _fail(msg)
    conn = _conn(cfg)
    try:
        result = mod.run(conn, cfg, names, dry_run=dry_run)
    except DomainError as e:
        _fail(str(e))
    finally:
        conn.close()

    results = result["sources"]
    failed = [r for r in results if r["outcome"] == "error"]
    if as_json:
        _echo_json(result)
    else:
        shown = [r for r in results if r["outcome"] != "skipped"]
        where = "on this computer" if result["auto"] else "named"
        head = (f"dry run — nothing written; {len(shown)} source(s) {where}:" if dry_run
                else f"collected {len(shown)} source(s) {where} — run "
                     f"{result['run_uuid'][:8]}")
        typer.echo(head if shown else "collect: nothing on this computer to collect yet"
                   " — python -m lifecrm collect setup shows how to get the phone's "
                   "data here")
        for r in shown:
            typer.echo(_line(r, dry_run))
        _skipped_lines(results)
        waiting = [r["source"] for r in shown if r["outcome"] == "held"]
        if waiting and not dry_run:
            typer.echo(f"waiting for your yes: {', '.join(waiting)} — /morning asks; or: "
                       f"python -m lifecrm collect approve <source>")
    if failed:
        raise typer.Exit(1)


def _skipped_lines(results: list[dict]) -> None:
    by: dict[str, list[dict]] = {}
    for r in results:
        if r["outcome"] == "skipped":
            by.setdefault(r.get("skip", ""), []).append(r)
    if by.get("not found"):
        typer.echo("not on this computer: " + ", ".join(r["source"] for r in by["not found"])
                   + " — python -m lifecrm collect setup shows how to add the phone's")
    if by.get("declined"):
        typer.echo("declined: " + ", ".join(r["source"] for r in by["declined"])
                   + " — python -m lifecrm collect approve <source> takes one back")
    for r in by.get("unreadable", []):
        typer.echo(f"found but not readable, so not offered: {r['source']} — {r['reason']}")
    if by.get("empty"):
        typer.echo("found but empty: " + ", ".join(r["source"] for r in by["empty"]))


@collect_app.command("status")
def status_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """Each source's last outcome, what was declined, when a run last started."""
    _status(as_json)


def _status(as_json: bool) -> None:
    from .. import collect as mod
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..domain.util import DomainError
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        report = mod.status(conn, cfg)
    except DomainError as e:
        _fail(str(e))
    except sqlite3.OperationalError as e:
        _fail(f"cannot read the collect history ({e}) — run: python -m lifecrm migrate")
    finally:
        conn.close()
    if as_json:
        _echo_json(report)
        return
    last = report["last_run_at"]
    scope = ("every source on this computer" if report["auto"]
             else f"{len(report['configured'])} source(s) in [collect] sources")
    typer.echo(f"collect: {scope}; "
               + (f"last run {last[:16].replace('T', ' ')} UTC" if last else "never run"))
    for p in report.get("phone") or []:
        typer.echo(f"  phone: {p['name']} shown in File Explorer — "
                   + ("reachable" if p["reachable"] else
                      "not answering (on the same Wi-Fi as this computer?)"))
    if not report["sources"]:
        typer.echo("  nothing collected yet — python -m lifecrm collect setup shows what "
                   "this computer has")
    for s in report["sources"]:
        row = s["latest"]
        mark = " (declined)" if s["declined"] else (
            "" if s["configured"] else " (no longer in [collect] sources)")
        if row is None:
            typer.echo(f"  {s['source']:<15} never collected{mark}")
            continue
        when = row["started_at"][:16].replace("T", " ")
        what = (counts_text(row["counts"]) if row["outcome"] in ("applied", "unchanged")
                else row["reason"])
        applied = (f" · last applied {s['last_applied'][:10]}"
                   if s["last_applied"] and row["outcome"] != "applied" else "")
        typer.echo(f"  {s['source']:<15} {row['outcome']:<10} {when}  {what}{applied}{mark}")


@collect_app.command("approve")
def approve_cmd(source: str = typer.Argument(..., help="The source to import, e.g. browser"),
                yes: bool = typer.Option(False, "--yes", "-y",
                                         help="Import without asking (after reading the "
                                              "preview)"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Yes to a source: shows what importing it now would bring in — the dry
    run's own report — then imports on your yes. A large removal the daily
    job held is approved the same way. From then on the job collects it."""
    from .. import collect as mod
    from ..cli import _echo_json, _fail
    from ..domain.util import DomainError
    cfg, conn = _open()
    try:
        preview = mod.approve(conn, cfg, source)
        if not as_json:
            for line in preview["report"]:
                typer.echo(line)
            if preview["target"]:              # None: an online service had nothing
                typer.echo(_summary(preview))
        if preview["nothing_new"]:
            _echo_json(preview) if as_json else typer.echo(
                "nothing new to import — the daily job already has it all")
            return
        if not yes:
            if as_json or not sys.stdin.isatty() or not _asked("Import this?"):
                if as_json:
                    _echo_json(preview)
                else:
                    typer.echo(f"nothing written. To import: python -m lifecrm collect "
                               f"approve {source} --yes")
                return
        result = mod.approve(conn, cfg, source, apply=True)
    except DomainError as e:
        _fail(str(e))
    finally:
        conn.close()
    if as_json:
        _echo_json(result)
        return
    typer.echo(f"imported {result['label']}: {counts_text(result['applied_counts'])}"
               + (f" · import {result['import_uuid'][:8]} (undo: python -m lifecrm "
                  f"imports retract {result['import_uuid']})" if result.get("import_uuid")
                  else ""))
    if result.get("snapshot"):
        typer.echo(f"snapshot before removing: {result['snapshot']}")
    typer.echo("from now on the daily job collects it by itself")


def _asked(question: str) -> bool:
    """A yes from the terminal; no answer at all (input closed, Ctrl+C,
    a shell that only looks like a terminal) is a no, never an abort."""
    try:
        return typer.confirm(question)
    except (typer.Abort, EOFError, KeyboardInterrupt):
        typer.echo("")
        return False


def _summary(p: dict) -> str:
    span = f" from {p['span'][0]} to {p['span'][1]}" if p.get("span") else ""
    line = (f"{p['label']}: {p['counts'].get('new', 0):,} new record(s){span}, in "
            f"{p['target']}")
    if p.get("mass_removal"):
        line += f"; it also removes what the source no longer holds — {p['mass_removal']}"
    if p.get("first_import"):
        line += f"; first import — what to check: {p['doc']}" + (
            " (its first-real-import checklist)" if p.get("checklist") else "")
    return line


@collect_app.command("decline")
def decline_cmd(source: str = typer.Argument(..., help="The source never to collect"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """No to a source, for good: the daily job stops looking at it and stops
    asking. `collect approve` takes it back."""
    from .. import collect as mod
    from ..cli import _echo_json, _fail
    from ..domain.util import DomainError
    cfg, conn = _open()
    try:
        result = mod.decline(conn, source)
    except DomainError as e:
        conn.rollback()
        _fail(str(e))
    finally:
        conn.close()
    if as_json:
        _echo_json(result)
        return
    typer.echo(f"declined: {source} — the daily job no longer looks at it. Take it back: "
               f"python -m lifecrm collect approve {source}")


@collect_app.command("setup")
def setup_cmd(phone: str = typer.Option(
                  None, "--phone", metavar="android|iphone-windows|iphone-mac",
                  help="Which setup (default: guessed from this computer)"),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """The phone's side, as a checklist: what is already collected, what is on
    this computer waiting for your yes, and the steps for the rest. Reads
    only."""
    from .. import collect as mod
    from ..cli import _echo_json, _fail
    from ..domain.util import DomainError
    cfg, conn = _open(readonly=True)
    try:
        data = mod.setup_checklist(conn, cfg, phone)
    except DomainError as e:
        _fail(str(e))
    finally:
        conn.close()
    if as_json:
        _echo_json(data)
        return
    guessed = "" if phone else " (guessed from this computer; --phone to change it)"
    typer.echo(f"Phone setup — {data['title']}{guessed}")
    for p in data.get("linked") or []:
        typer.echo(f"  {p['name']} is shown in this computer's File Explorer"
                   + (": its own app folders are read directly, no sync app needed "
                      "(docs/android.md)" if p["reachable"] else
                      ", but is not answering now — on the same Wi-Fi as this computer?"))
    for it in data["items"]:
        state = it["state"]
        mark = _SETUP_MARKS.get(state.split(" (")[0], "     ")
        if state.startswith("collecting"):
            text = "collected every morning"
        elif state.startswith("waiting"):
            text = (f"on this computer — import it: python -m lifecrm collect approve "
                    f"{it['source']}")
        elif state.startswith("declined"):
            text = "you declined it"
        elif state.startswith("blocked"):
            text = "on this computer, but macOS blocks reading it — Full Disk Access:"
        else:
            text = "not on this computer yet:"
        extra = state[len(state.split(' (')[0]):]
        typer.echo(f"  [{mark}] {it['what']:<46} {text}{extra}")
        for i, step in enumerate(it["steps"], 1):
            typer.echo(f"            {i}. {step}")
    if data.get("online"):
        typer.echo("Online — fetched by the daily job once connected (docs/online.md):")
        for s in data["online"]:
            mark = "done " if s["connected"] else "to do"
            typer.echo(f"  [{mark}] {s['title']:<46} "
                       + ("connected" if s["connected"] else
                          f"python -m lifecrm connect {s['service']}:"))
            for i, step in enumerate(s["steps"], 1):
                typer.echo(f"            {i}. {step}")
    typer.echo("Each one you finish shows up the next morning: /morning asks whether "
               "to import it.")
    typer.echo(f"The whole guide: {data['guide']}")

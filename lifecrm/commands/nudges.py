"""`lifecrm nudges` — what matters today, cited; and the owner's "not now".

The list is built in `lifecrm/nudges.py` from a read-only connection; this
module owns the flags and the four writes (snooze, dismiss, restore by
durable key, through lifecrm/domain/nudges.py and the CLI's commit-or-
rollback). A numbered `N#` is accepted for a write only together with the
saved pack it came from (`--pack`), because the numbers of one run mean
nothing in the next. `--check` reads a brief against that same saved pack,
never against a rebuild. docs/assistant.md.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import typer

nudges_app = typer.Typer(
    help="What the records say matters today (reminders, birthdays, nutrients, "
         "stale imports), cited; snooze or dismiss by key.",
    invoke_without_command=True)


def register(app: typer.Typer) -> None:
    app.add_typer(nudges_app, name="nudges")


def _load_pack(path: str) -> dict:
    from ..cli import _fail
    try:
        pack = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _fail(f"cannot read the saved pack {path}: {e}")
    if not isinstance(pack, dict) or "index" not in pack or "pack_id" not in pack:
        _fail(f"{path} is not a pack written by: python -m lifecrm nudges --out FILE")
    return pack


def _resolve_key(ref: str, pack_path: str | None) -> str:
    """A durable key as given; an N# only through the pack it was read from."""
    from ..cli import _fail
    if ":" in ref:
        return ref
    if not pack_path:
        _fail(f"{ref} is a number from one run — pass the key printed beside it "
              f"(due:<uuid>, birthday:<id>:<year>, ...) or add --pack <file written "
              f"by nudges --out> so {ref} resolves against that run")
    item = _load_pack(pack_path)["index"].get(ref.upper())
    if not item or item.get("type") != "nudge":
        _fail(f"{ref} is not a nudge in {pack_path}")
    return item["key"]


@nudges_app.callback()
def nudges(ctx: typer.Context,
           as_json: bool = typer.Option(False, "--json", help="Print the pack as JSON"),
           show_all: bool = typer.Option(False, "--all",
                                         help="Also list snoozed and dismissed nudges"),
           today: str = typer.Option(None, "--today", metavar="YYYY-MM-DD",
                                     help="Stand in for today (default: the clock, "
                                          "with the configured rollover)"),
           out: str = typer.Option(None, "--out", metavar="FILE",
                                   help="Also save the pack as JSON (what --check and "
                                        "N# references read)"),
           check: str = typer.Option(None, "--check", metavar="BRIEF",
                                     help="Check a brief's citations against --pack"),
           pack_file: str = typer.Option(None, "--pack", metavar="FILE",
                                         help="The saved pack a brief was written from"),
           patterns: bool = typer.Option(None, "--patterns/--no-patterns",
                                         help="Include the pattern finder's block "
                                              "(experimental; default: [assistant] patterns)")
           ) -> None:
    """List what matters today, cited (default when no sub-command is given)."""
    if ctx.invoked_subcommand is not None:
        return
    from .. import nudges as mod
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..domain.util import DomainError

    if check:
        if not pack_file:
            _fail("--check needs --pack <file>: a brief is checked against the saved "
                  "pack it was written from, never a rebuild")
        pack = _load_pack(pack_file)
        try:
            text = Path(check).read_text(encoding="utf-8")
        except OSError as e:
            _fail(f"cannot read {check}: {e}")
        result = mod.check(text, pack)
        ok = not result["unknown"] and not result["uncited"] and not result["unsupported"]
        if as_json:
            _echo_json({**result, "ok": ok, "pack_id": pack["pack_id"]})
        else:
            if result["unknown"]:
                typer.echo(f"unknown id(s): {', '.join(result['unknown'])}")
            if result["uncited"]:
                typer.echo("the brief cites nothing from the pack")
            for s in result["unsupported"]:
                typer.echo(f"unsupported (a number or date not in its cited lines): {s}")
            typer.echo(("ok: " if ok else "FAILED: ")
                       + f"{len(result['cited'])} id(s) cited, pack {pack['pack_id'][:12]}")
        raise typer.Exit(0 if ok else 1)

    cfg = _cfg()
    try:
        day, now = mod.today_and_now(cfg, today)
    except DomainError as e:
        _fail(str(e))
    conn = _conn(cfg, readonly=True)
    try:
        pack = mod.build(conn, cfg, today=day, now=now, patterns=patterns)
    except DomainError as e:
        _fail(str(e))
    except sqlite3.Error as e:
        _fail(f"nudges failed reading {cfg.db_path}: {e}")
    finally:
        conn.close()
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8",
                        newline="\n")
    if as_json:
        _echo_json(pack)
        return
    for line in mod.render_lines(pack, show_hidden=show_all):
        typer.echo(line)
    if out:
        typer.echo(f"pack saved: {out} (pack {pack['pack_id'][:12]})")


def _write(fn) -> dict:
    from ..cli import _cfg, _conn, _commit_or_rollback
    cfg = _cfg()
    conn = _conn(cfg)
    return _commit_or_rollback(conn, lambda: fn(conn, cfg))


@nudges_app.command("snooze")
def snooze(ref: str = typer.Argument(..., metavar="KEY|N#",
                                     help="The nudge's key, or its N# with --pack"),
           until: str = typer.Option(None, "--until", metavar="YYYY-MM-DD",
                                     help="Show it again on this day"),
           days: int = typer.Option(None, "--days", help="Show it again in N days"),
           note: str = typer.Option(None, "--note"),
           pack_file: str = typer.Option(None, "--pack", metavar="FILE")) -> None:
    """Hide a nudge until a day (--until) or for N days (--days)."""
    from .. import nudges as mod
    from ..cli import _cfg, _fail
    from ..domain import nudges as state
    if (until is None) == (days is None):
        _fail("give --until YYYY-MM-DD or --days N, one of them")
    key = _resolve_key(ref, pack_file)
    if days is not None:
        if days < 1:
            _fail("--days must be at least 1")
        today, _ = mod.today_and_now(_cfg())
        until = (today + timedelta(days=days)).isoformat()
    row = _write(lambda conn, cfg: state.snooze(conn, key, until=until, note=note))
    typer.echo(f"snoozed {row['nudge_key']} until {row['until']}")


@nudges_app.command("dismiss")
def dismiss(ref: str = typer.Argument(..., metavar="KEY|N#"),
            note: str = typer.Option(None, "--note"),
            pack_file: str = typer.Option(None, "--pack", metavar="FILE")) -> None:
    """Hide a nudge for good (until `nudges restore`)."""
    from ..domain import nudges as state
    key = _resolve_key(ref, pack_file)
    row = _write(lambda conn, cfg: state.dismiss(conn, key, note=note))
    typer.echo(f"dismissed {row['nudge_key']}")


@nudges_app.command("restore")
def restore(key: str = typer.Argument(..., metavar="KEY")) -> None:
    """Forget a snooze or dismissal; the nudge shows again if it still holds."""
    from ..domain import nudges as state
    row = _write(lambda conn, cfg: state.restore(conn, key))
    typer.echo(f"restored {row['nudge_key']} (was {row['was']})")


@nudges_app.command("states")
def states(as_json: bool = typer.Option(False, "--json")) -> None:
    """List every snoozed or dismissed nudge key."""
    from ..cli import _cfg, _conn, _echo_json
    from ..domain import nudges as state
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        rows = state.states(conn)
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
        return
    if not rows:
        typer.echo("no snoozed or dismissed nudges")
    for r in rows:
        how = r["state"] + (f" until {r['until']}" if r["until"] else "")
        typer.echo(f"{r['nudge_key']}  {how}" + (f"  — {r['note']}" if r["note"] else ""))

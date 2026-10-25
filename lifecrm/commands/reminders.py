"""`lifecrm reminders publish` — the reminders calendar, regenerated.

The calendar itself is built in `lifecrm/reminders.py`, the one publisher;
this module owns the flags. The same publication runs by itself after every
CLI command that changed the database (when `[assistant] reminders_dir` is
set) and every morning from scripts/reminders.*, so this command is for the
first time, for a check, and for a machine that runs neither.
docs/assistant.md.
"""

from __future__ import annotations

import sqlite3

import typer

reminders_app = typer.Typer(
    help="The reminders calendar (.ics): due tasks and plans, birthdays.",
    no_args_is_help=True)


def register(app: typer.Typer) -> None:
    app.add_typer(reminders_app, name="reminders")


@reminders_app.command("publish")
def publish(today: str = typer.Option(None, "--today", metavar="YYYY-MM-DD",
                                      help="Stand in for today (default: the clock)"),
            now: str = typer.Option(None, "--now", metavar="ISO-UTC",
                                    help="The publication instant stamped on changed "
                                         "components (default: the clock)"),
            out: str = typer.Option(None, "--out", metavar="FILE",
                                    help="Also write the calendar here"),
            dry_run: bool = typer.Option(False, "--dry-run",
                                         help="Say what would change, write nothing"),
            if_configured: bool = typer.Option(
                False, "--if-configured",
                help="Exit 0 with one line when no reminders folder is set (for the "
                     "scheduled job)"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Regenerate the calendar, record its revisions, write the file."""
    from datetime import datetime, timezone
    from .. import nudges as nudges_mod
    from .. import reminders as mod
    from ..cli import _cfg, _echo_json, _fail
    from ..domain.util import DomainError

    cfg = _cfg()
    try:
        day, instant = nudges_mod.today_and_now(cfg, today)
        if now:
            try:
                instant = datetime.fromisoformat(now.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=timezone.utc)
            except ValueError:
                _fail(f"--now must be an ISO instant like 2026-10-24T13:00:00Z, got {now!r}")
        s = nudges_mod.settings(cfg)
        if if_configured and not s["reminders_dir"] and not out:
            msg = ("reminders: no [assistant] reminders_dir in config.toml — nothing "
                   "published (docs/assistant.md)")
            _echo_json({"skipped": True, "reason": msg}) if as_json else typer.echo(msg)
            return
        result = mod.publish(cfg, today=day, now=instant, out=out, dry_run=dry_run)
    except DomainError as e:
        _fail(str(e))
    except sqlite3.Error as e:
        _fail(f"reminders publish failed on {cfg.db_path}: {e}")
    if as_json:
        _echo_json(result)
    else:
        moved = ", ".join(f"{result[k]} {k}" for k in ("added", "changed", "restored",
                                                       "removed") if result[k])
        if result["dry_run"]:
            typer.echo(f"would publish {result['components']} component(s) "
                       f"({moved or 'no revisions'}) to {result['path']}")
        else:
            typer.echo(f"published {result['components']} component(s) "
                       f"({moved or 'no revisions'}): {result['path']}"
                       + ("" if result["wrote"] else " (unchanged)"))
            for c in result["copies"]:
                typer.echo(f"copied to {c}")
    for w in result.get("warnings", []):
        typer.echo(f"warning: {w}", err=True)

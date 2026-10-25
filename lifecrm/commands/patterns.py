"""`lifecrm patterns` — co-occurrence between daily series. EXPERIMENTAL.

The finder lives in `lifecrm/patterns.py`; this module owns the flags and
the banner. Off everywhere else by default (the nudges pack, the review
pack, /morning) until the validation suite has passed; `[assistant]
patterns = "on"` turns it on there. docs/patterns.md.
"""

from __future__ import annotations

import sqlite3

import typer


def register(app: typer.Typer) -> None:
    app.command("patterns")(patterns)


def patterns(
    days: int = typer.Option(None, "--days", help="The window ending today (default 90)"),
    today: str = typer.Option(None, "--today", metavar="YYYY-MM-DD",
                              help="Stand in for today (default: the clock)"),
    keys: str = typer.Option(None, "--keys",
                             help="Comma-separated series keys (default: the registry's "
                                  "pattern defaults)"),
    all_tested: bool = typer.Option(False, "--all",
                                    help="Also print every pair tested and why it was not "
                                         "reported"),
    no_check: bool = typer.Option(False, "--no-check", help="Skip the day-shift check"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Which daily series go together, same day or next day — statistics over
    your own records, co-occurrence never cause, experimental."""
    from .. import nudges as nudges_mod
    from .. import patterns as mod
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..domain.util import DomainError

    cfg = _cfg()
    try:
        day, _now = nudges_mod.today_and_now(cfg, today)
    except DomainError as e:
        _fail(str(e))
    conn = _conn(cfg, readonly=True)
    try:
        result = mod.find(conn, cfg, end=day, days=days or mod.DEFAULT_DAYS,
                          keys=[k.strip() for k in keys.split(",") if k.strip()] if keys
                          else None, check=not no_check)
    except DomainError as e:
        _fail(str(e))
    except sqlite3.Error as e:
        _fail(f"patterns failed reading {cfg.db_path}: {e}")
    finally:
        conn.close()
    if as_json:
        _echo_json(result)
        return
    for line in mod.render_lines(result, show_all=all_tested):
        typer.echo(line)

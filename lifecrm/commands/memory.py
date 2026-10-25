"""`lifecrm onthisday`, `prompts`, `highlights` — what earlier years held on a
date, a day's moments worth a word in the journal, and the day's highlights
(lifecrm/memories.py, prompts.py, highlights.py). Read-only. The same
sections ride the nudges pack for /morning. docs/features.md.
"""

from __future__ import annotations

import sqlite3

import typer


def register(app: typer.Typer) -> None:
    app.command("onthisday")(onthisday)
    app.command("prompts")(prompts)
    app.command("highlights")(highlights)


def _day(cfg, value: str | None, *, default_offset: int = 0):
    from datetime import timedelta

    from .. import nudges
    from ..cli import _fail
    from ..domain.util import DomainError
    try:
        d, _now = nudges.today_and_now(cfg, value)
    except DomainError as e:
        _fail(str(e))
    return d if value else d - timedelta(days=default_offset)


def _read(cfg, fn):
    from ..cli import _conn, _fail
    conn = _conn(cfg, readonly=True)
    try:
        return fn(conn)
    except sqlite3.Error as e:
        _fail(f"failed reading {cfg.db_path}: {e}")
    finally:
        conn.close()


def onthisday(day: str = typer.Option(None, "--date", metavar="YYYY-MM-DD",
                                      help="Another day (default: today)"),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """Earlier years on this month and day: your entries, and the photos,
    places, meetings, workouts, highlights and texts your imports hold."""
    from .. import memories
    from ..cli import _cfg, _echo_json
    from .places import need
    cfg = _cfg()
    need(cfg, "on_this_day")
    d = _day(cfg, day)
    data = _read(cfg, lambda conn: memories.on_this_day(conn, cfg, d))
    if as_json:
        _echo_json(data)
        return
    if not data["years"]:
        typer.echo(f"nothing recorded on {d.strftime('%B')} {d.day} in an earlier year")
        return
    typer.echo(f"On this day ({d.strftime('%B')} {d.day}) in earlier years:")
    for y in data["years"]:
        typer.echo(f"  {y['date']} ({y['years_ago']} year(s) ago)")
        if y["summary"]:
            typer.echo(f"    journal: {y['summary']}")
        for e in y["entries"]:
            typer.echo(f"    - {e['kind']}: {e['title']} · entry {e['uuid']}")
        for r in y["records"]:
            ref = r.get("uuid") or r.get("first_uuid")
            typer.echo(f"    - {r['text']} (observed) · {ref}")


def prompts(day: str = typer.Option(None, "--date", metavar="YYYY-MM-DD",
                                    help="The journal day (default: yesterday)"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Moments in a day's imports worth a word in the journal — a first visit,
    photos, a long call, a meeting, a workout, an unusual spend. A prompt,
    never an entry: only what you then say becomes one."""
    from .. import prompts as prompts_mod
    from ..cli import _cfg, _echo_json
    from .places import need
    cfg = _cfg()
    need(cfg, "journal_prompts")
    d = _day(cfg, day, default_offset=1)
    data = _read(cfg, lambda conn: prompts_mod.moments(conn, cfg, d.isoformat()))
    if as_json:
        _echo_json(data)
        return
    for line in prompts_mod.render_lines(data):
        typer.echo(line)


def highlights(day: str = typer.Option(None, "--date", metavar="YYYY-MM-DD",
                                       help="Another day's share (default: today)"),
               per_day: int = typer.Option(3, "--per-day", min=1, max=20),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """A few of your book highlights, in a fixed rotation (every highlight
    comes round once per cycle; nothing is recorded)."""
    from .. import highlights as hl
    from ..cli import _cfg, _echo_json
    from .places import need
    cfg = _cfg()
    need(cfg, "highlights")
    d = _day(cfg, day)
    data = _read(cfg, lambda conn: hl.for_day(conn, d, per_day))
    if as_json:
        _echo_json(data)
        return
    for line in hl.render_lines(data):
        typer.echo(line)

"""`lifecrm connect` — the one-time setup of an online source (docs/online.md).

    lifecrm connect                     which services there are, which are connected
    lifecrm connect SERVICE [ARGS]...   set one up (asks for what it needs)
    lifecrm disconnect SERVICE          forget what it kept (fetched files stay)
    lifecrm fetch SERVICE               download its new data now, import nothing

The services live in lifecrm/online/; this module owns the prompts and the
report. A password or a private link is typed by the owner into their own
terminal and kept in data/secrets.json — never echoed, never printed back.
"""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    app.command("connect")(connect)
    app.command("disconnect")(disconnect)
    app.command("fetch")(fetch)


def _ask(prompt: str, hide: bool = False) -> str:
    return typer.prompt(prompt, hide_input=hide)


def connect(service: str = typer.Argument(None, help="The service to set up — leave out "
                                                     "to list them"),
            args: list[str] = typer.Argument(None, metavar="[ARGS]...",
                                             help="What the service takes (e.g. links)"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Connect an online source once — from then on the daily collect job
    fetches it by itself (docs/online.md)."""
    from .. import online
    from ..cli import _cfg, _echo_json, _fail
    from ..domain.util import DomainError
    cfg = _cfg()
    if service is None:
        rows = online.status(cfg)
        if as_json:
            _echo_json(rows)
            return
        for r in rows:
            typer.echo(f"  [{'connected' if r['connected'] else '   -    '}] "
                       f"{r['service']:<16} {r['title']} (read by: {r['connector']})")
            if not r["connected"]:
                for i, step in enumerate(r["steps"], 1):
                    typer.echo(f"        {i}. {step}")
        return
    try:
        result = online.connect(cfg, service, args or [], _ask)
    except DomainError as e:
        _fail(str(e))
    if as_json:
        _echo_json(result)
        return
    typer.echo(f"connected: {result['title']} — the next collect run fetches it, and "
               f"/morning asks before its first import (preview it now: python -m "
               f"lifecrm collect approve {result['connector']})")


def disconnect(service: str = typer.Argument(..., help="The service to forget")) -> None:
    """Forget what a service kept (its link, password or token). What it
    fetched stays in data/fetched/, and what was imported stays imported."""
    from .. import online
    from ..cli import _cfg, _fail
    from ..domain.util import DomainError
    cfg = _cfg()
    try:
        gone = online.disconnect(cfg, service)
    except DomainError as e:
        _fail(str(e))
    typer.echo(f"disconnected: {service}" if gone else f"{service} was not connected")


def fetch(service: str = typer.Argument(..., help="The service to fetch from now"),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Download a connected service's new data now, without importing it —
    `collect approve <connector>` or the next morning's run reads it."""
    from .. import online
    from ..cli import _cfg, _echo_json, _fail
    from ..domain.util import DomainError
    cfg = _cfg()
    try:
        mod = online.get(service)
        target = online.fetch(cfg, mod)
    except DomainError as e:
        _fail(str(e))
    if as_json:
        _echo_json({"service": service, "target": str(target) if target else None})
        return
    if target is None:
        typer.echo(f"nothing new from {mod.TITLE}")
        return
    typer.echo(f"fetched {mod.TITLE} into {target} — nothing imported; read it with: "
               f"python -m lifecrm collect approve {mod.CONNECTOR}")

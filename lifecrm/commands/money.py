"""`lifecrm subscriptions` and `lifecrm experiments` — recurring charges and
self-experiments, each read from the records as of a day
(lifecrm/subscriptions.py, lifecrm/experiments.py). Read-only.
"""

from __future__ import annotations

import sqlite3

import typer


def register(app: typer.Typer) -> None:
    app.command("subscriptions")(subscriptions)
    app.command("experiments")(experiments)


def subscriptions(today: str = typer.Option(None, "--today", metavar="YYYY-MM-DD",
                                            help="Stand in for today"),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """Charges that repeat weekly, monthly, quarterly or yearly, from your
    statements' own descriptions: amount, next charge, price rises."""
    from .. import subscriptions as subs
    from ..cli import _cfg, _echo_json
    from .memory import _day, _read
    from .places import need
    cfg = _cfg()
    need(cfg, "subscriptions")
    d = _day(cfg, today)
    data = _read(cfg, lambda conn: subs.detect(conn, cfg, d))
    if as_json:
        _echo_json(data)
        return
    for line in subs.render_lines(data):
        typer.echo(line)


def experiments(today: str = typer.Option(None, "--today", metavar="YYYY-MM-DD",
                                          help="Stand in for today"),
                show_all: bool = typer.Option(False, "--all",
                                              help="Also experiments whose plan is closed"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Plans with data.experiment: the outcome during the change against the
    days before it, over known days only. Co-occurrence, never cause."""
    from .. import experiments as exp_mod
    from ..cli import _cfg, _echo_json
    from .memory import _day, _read
    from .places import need
    cfg = _cfg()
    need(cfg, "experiments")
    d = _day(cfg, today)

    def run(conn: sqlite3.Connection):
        return exp_mod.current(conn, cfg, d, include_closed=show_all)

    items = _read(cfg, run)
    if as_json:
        _echo_json([{k: v for k, v in e.items() if k not in ("baseline", "during")}
                    | ({"baseline": {k: v for k, v in e["baseline"].items() if k != "refs"},
                        "during": {k: v for k, v in e["during"].items() if k != "refs"}}
                       if "baseline" in e else {}) for e in items])
        return
    for line in exp_mod.render_lines(items, d):
        typer.echo(line)

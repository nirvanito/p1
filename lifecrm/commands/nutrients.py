"""`lifecrm nutrients`: average intake beside the reference intakes, cited.

The report itself is built in `lifecrm/nutrients.py`; this module owns only
the flags and the output. The connection is read-only and no model is
involved: the same database and arguments give the same report, byte for
byte, and nothing here can change a record.
"""

from __future__ import annotations

import sqlite3

import typer


def register(app: typer.Typer) -> None:
    app.command("nutrients")(nutrients)


def nutrients(
    days: int = typer.Option(None, "--days",
                             help="The N days ending today (default 30)"),
    start: str = typer.Option(None, "--start", metavar="YYYY-MM-DD",
                              help="First day of a custom window"),
    end: str = typer.Option(None, "--end", metavar="YYYY-MM-DD",
                            help="Last day (default: today)"),
    sex: str = typer.Option(None, "--sex",
                            help='female | male (default: [nutrition] sex)'),
    age: int = typer.Option(None, "--age",
                            help="Age in years, 19 or over (default: [nutrition] age)"),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON"),
) -> None:
    """Compare logged intake with adult Dietary Reference Intakes.

    Averages each nutrient per day over the window from the nutrition
    connector's daily summaries (summing food_log servings for a day without
    one), and lists what is below 70% of the RDA or AI, 70-100%, and above
    an upper level — with the foods behind each and the observation ids
    every number came from. A comparison with reference values, not medical
    advice. See docs/nutrition.md.
    """
    from .. import nutrients as mod
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..domain.util import DomainError

    cfg = _cfg()
    try:
        window = mod.resolve_window(cfg, days=days, start=start, end=end)
        prof = mod.profile(cfg, sex=sex, age=age)
    except DomainError as e:
        _fail(str(e))
    conn = _conn(cfg, readonly=True)
    try:
        report = mod.build_report(conn, cfg, window, prof=prof)
    except DomainError as e:
        _fail(str(e))
    except sqlite3.Error as e:
        _fail(f"nutrients failed reading {cfg.db_path}: {e}")
    finally:
        conn.close()
    if as_json:
        _echo_json(report)
        return
    for line in mod.render_lines(report):
        typer.echo(line)

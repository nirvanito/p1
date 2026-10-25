"""`lifecrm places` — town names, trips and countries from a local GeoNames
file (lifecrm/places.py). `fetch` downloads that file once, the only network
access the feature has; everything else reads the database. docs/places.md.
"""

from __future__ import annotations

import sqlite3

import typer

places_app = typer.Typer(help="Town names, trips away from home and countries visited, "
                              "from a local GeoNames file (the places feature).",
                         no_args_is_help=True)


def register(app: typer.Typer) -> None:
    app.add_typer(places_app, name="places")


def need(cfg, key: str) -> None:
    """Refuse, in one line, while a feature is unchecked."""
    from .. import features
    from ..cli import _fail
    from ..domain.util import DomainError
    try:
        on = features.enabled(cfg, key)
    except DomainError as e:
        _fail(str(e))
    if not on:
        _fail(f"the {features.BY_KEY[key].title.lower()} feature is unchecked — run: "
              f"python -m lifecrm features check {key}")


def _timeline(cfg):
    from .. import places
    from ..cli import _conn, _fail
    need(cfg, "places")
    conn = _conn(cfg, readonly=True)
    try:
        tl = places.timeline(conn, cfg)
    except sqlite3.Error as e:
        _fail(f"places failed reading {cfg.db_path}: {e}")
    finally:
        conn.close()
    if not tl["ok"]:
        _fail(tl["error"])
    return tl


@places_app.command("status")
def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """Where the gazetteer is and how many towns it holds."""
    from .. import places
    from ..cli import _cfg, _echo_json
    cfg = _cfg()
    st = places.status(cfg)
    if as_json:
        _echo_json(st)
        return
    if st["ok"]:
        typer.echo(f"gazetteer: {st['path']} — {st['towns']:,} towns ({st['source']}); "
                   f"names: {places.ATTRIBUTION}")
    else:
        typer.echo(f"no gazetteer: {st['error']}")


@places_app.command("fetch")
def fetch(dataset: str = typer.Option(
              "cities1000", "--dataset",
              help="GeoNames file: cities500 | cities1000 | cities5000 | cities15000 "
                   "(towns over that many people)"),
          dry_run: bool = typer.Option(False, "--dry-run",
                                       help="Say what would be downloaded; download nothing")
          ) -> None:
    """Download GeoNames' towns once into data/geo/places.tsv (CC BY 4.0).
    The only network access the places feature has."""
    from .. import places
    from ..cli import _cfg, _fail
    from ..domain.util import DomainError
    cfg = _cfg()
    try:
        result = places.fetch(cfg, dataset=dataset, dry_run=dry_run)
    except DomainError as e:
        _fail(str(e))
    except OSError as e:
        _fail(f"could not write the gazetteer: {e}")
    if result["dry_run"]:
        typer.echo("would download: " + ", ".join(result["downloads"]))
        typer.echo(f"into: {result['target']}")
        return
    typer.echo(f"wrote {result['target']}: {result['towns']:,} towns, "
               f"{result['bytes'] / 1e6:.1f} MB (names: {places.ATTRIBUTION})")


@places_app.command("trips")
def trips(as_json: bool = typer.Option(False, "--json"),
          year: int = typer.Option(None, "--year", help="Only trips in this year")) -> None:
    """Trips away from home, newest first."""
    from .. import places
    from ..cli import _cfg, _echo_json
    cfg = _cfg()
    tl = _timeline(cfg)
    found = tl["trips"]
    if year:
        found = places.trips_between(tl, f"{year}-01-01", f"{year}-12-31")
    if as_json:
        _echo_json([places.json_safe_trip(t) for t in found])
        return
    typer.echo(f"{len(found)} trip(s): days with a located record more than "
               f"{tl['trip_km']:g} km from home (observed; towns from GeoNames)")
    for t in reversed(found):
        typer.echo(f"  - {places.describe_trip(t)} · first record {t['first_uuid']}")


@places_app.command("countries")
def countries(as_json: bool = typer.Option(False, "--json")) -> None:
    """Every country a located record falls in, first visit first."""
    from ..cli import _cfg, _echo_json
    tl = _timeline(_cfg())
    if as_json:
        _echo_json([{k: v for k, v in c.items() if not k.endswith("_obs_id")}
                    for c in tl["countries"]])
        return
    typer.echo(f"{len(tl['countries'])} countr{'y' if len(tl['countries']) == 1 else 'ies'} "
               f"with located records (observed):")
    for c in tl["countries"]:
        typer.echo(f"  - {c['name']} ({c['cc']}): {c['first_date']} to {c['last_date']}, "
                   f"{c['days']} day(s); {', '.join(c['towns'][:8])}")


@places_app.command("where")
def where(day: str = typer.Argument(..., metavar="YYYY-MM-DD"),
          end: str = typer.Option(None, "--end", metavar="YYYY-MM-DD",
                                  help="Through this day")) -> None:
    """The towns your located records put you near, day by day."""
    from ..cli import _cfg, _fail
    from ..domain.util import is_iso_date
    if not is_iso_date(day) or (end and not is_iso_date(end)):
        _fail("dates must be YYYY-MM-DD")
    tl = _timeline(_cfg())
    last = end or day
    shown = 0
    for d in sorted(tl["days"]):
        if day <= d <= last:
            info = tl["days"][d]
            towns = ", ".join(t.label for t in info["towns"]) or "no town within range"
            away = f" — away, {info['far_km']:g} km from home" if info["away"] else ""
            typer.echo(f"  {d}: {towns}{away}")
            shown += 1
    if not shown:
        typer.echo(f"no located record from {day} to {last}")

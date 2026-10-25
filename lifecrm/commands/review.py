"""`lifecrm review`: the weekly evidence pack, and the check a brief must pass.

The pack itself is built in `lifecrm/review.py`; this module owns only the
flags, the files in data/staging and the output. Reading is all it does to
the database — the connection is read-only — so the scheduled weekly run can
never change a record, and a brief only ever lands through `import` +
`ingest` + `apply` like any other note.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    app.command("review")(review)


def review(
    lens: str = typer.Option(
        None, "--lens",
        help="week | health | career | relationships | finance | all | year "
             "(default week; year with --year)"),
    week: str = typer.Option(None, "--week", metavar="YYYY-Www",
                             help="An ISO week, e.g. 2026-W42"),
    year: int = typer.Option(None, "--year", metavar="YYYY",
                             help="A calendar year (the year in review: --lens year)"),
    start: str = typer.Option(None, "--start", metavar="YYYY-MM-DD",
                              help="First day of a custom period"),
    end: str = typer.Option(None, "--end", metavar="YYYY-MM-DD",
                            help="Last day (default: today)"),
    days: int = typer.Option(None, "--days",
                             help="The N days ending today"),
    quiet_weeks: int = typer.Option(
        None, "--quiet-weeks",
        help="Relationships: list people not contacted for this many weeks "
             "(default 3, or [review] quiet_weeks)"),
    check: str = typer.Option(
        None, "--check", metavar="BRIEF",
        help="Verify a brief's [E1]-style citations against this pack"),
    out: str = typer.Option(
        None, "--out", metavar="FILE",
        help="With --check: write the brief plus a sources footer here, "
             "ready for `lifecrm import`"),
    write_only: bool = typer.Option(
        False, "--write-only",
        help="Write the pack files and print only their paths (scheduler)"),
    as_json: bool = typer.Option(False, "--json", help="Print the pack; write nothing"),
) -> None:
    """Build the evidence pack for a period: what happened, cited.

    Default period: the last full week, Monday to Sunday. Every item carries
    an id (D day, E entry, M journal metric, H health metric, O observation)
    that a brief must cite; nothing in the pack is interpretation. Writes
    data/staging/review-<lens>-<period>.md (and .json) unless --json.
    The /review-week and /advise skills turn the pack into a brief.
    """
    from .. import review as review_mod
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..domain.util import DomainError

    if lens is None:
        lens = "year" if year is not None else "week"
    if write_only and as_json:
        _fail("--write-only writes files and --json prints the pack — pick one")
    if out and not check:
        _fail("--out goes with --check BRIEF")
    if lens not in review_mod.LENSES:
        _fail(f"--lens '{lens}' must be one of: {', '.join(review_mod.LENSES)}")
    brief_text = None
    if check:
        brief = Path(check)
        if not brief.exists():
            _fail(f"file not found: {brief}")
        try:
            brief_text = brief.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as e:
            _fail(f"{brief.name} is not UTF-8 text (byte {e.start}: {e.reason}) — "
                  f"re-save it as UTF-8")

    cfg = _cfg()
    try:
        period = review_mod.resolve_period(cfg, week=week, start=start, end=end,
                                           days=days, year=year)
    except DomainError as e:
        _fail(str(e))
    conn = _conn(cfg, readonly=True)
    try:
        pack = review_mod.build_pack(conn, cfg, period, lens=lens,
                                     quiet_weeks=quiet_weeks)
    except DomainError as e:
        _fail(str(e))
    except sqlite3.Error as e:
        _fail(f"review failed reading {cfg.db_path}: {e}")
    finally:
        conn.close()

    if brief_text is not None:
        _check(pack, brief_text, out, as_json)
        return
    if as_json:
        _echo_json(pack)
        return

    stem = f"review-{lens}-{period['slug']}"
    md_path = cfg.staging_dir / f"{stem}.md"
    json_path = cfg.staging_dir / f"{stem}.json"
    try:
        cfg.staging_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(review_mod.render_markdown(pack), encoding="utf-8",
                           newline="\n")
        json_path.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8", newline="\n")
    except OSError as e:
        _fail(f"cannot write the pack to {cfg.staging_dir}: {e}")
    if write_only:
        typer.echo(str(md_path))
        typer.echo(str(json_path))
        return
    p, cov = pack["period"], pack["coverage"]
    loops = pack.get("loops")
    typer.echo(f"review pack: {p['label']} ({p['start']} to {p['end']}), lens {lens}")
    typer.echo(f"  {cov['entries']} entries on {cov['days_with_entries']} of "
               f"{cov['days_in_period']} day(s), {cov['health_records']} health "
               f"records, {cov['observations']} observations"
               + (f"; loops: {len(loops['opened'])} opened, {len(loops['closed'])} "
                  f"closed, {loops['open_total']} open, {len(loops['overdue'])} overdue"
                  if loops else ""))
    for note in pack["notes"]:
        typer.echo(f"  note: {note}")
    typer.echo(f"wrote {md_path}")
    typer.echo(f"  and {json_path}")
    typer.echo("turn it into a brief with /review-week (or /advise <lens>) in Claude Code")


def _check(pack: dict, text: str, out: str | None, as_json: bool) -> None:
    """Verify a brief against the pack; with --out, write the brief with its
    sources spelled out so the saved text stays checkable without the pack."""
    from .. import review as review_mod
    from ..cli import _echo_json, _fail

    result = review_mod.check_citations(text, pack)
    footer = review_mod.sources_footer(pack, result["cited"])
    if as_json:
        _echo_json({**result, "sources": footer,
                    "period": pack["period"], "lens": pack["lens"]})
        if result["unknown"] or result["uncited"]:
            raise typer.Exit(1)
        return
    if result["unknown"]:
        _fail(f"the brief cites {len(result['unknown'])} id(s) this pack does not "
              f"hold: {', '.join(result['unknown'])} — remove or correct those "
              f"claims (is it the same --lens and period?)")
    if result["uncited"]:
        _fail("the brief cites nothing from the pack — every claim needs an id "
              "like [E3] or [H1]")
    typer.echo(f"ok: the brief cites {len(result['cited'])} pack item(s): "
               f"{', '.join(result['cited'])}")
    if out:
        dest = Path(out)
        try:
            dest.write_text(text.rstrip() + "\n\n" + footer + "\n",
                            encoding="utf-8", newline="\n")
        except OSError as e:
            _fail(f"cannot write {dest}: {e}")
        typer.echo(f"wrote {dest} — import it with: python -m lifecrm import "
                   f"\"{dest}\" --type manual --date {pack['period']['end']}")

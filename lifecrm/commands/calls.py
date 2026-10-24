"""`lifecrm calls list | take | skip` — call recordings, transcribed only by choice.

Intake holds every call recording it finds in the audio inbox
(lifecrm/transcribe.py: Samsung's "Call recording <name>_yyMMdd_HHmmss",
"Call with ...", [intake] call_patterns). Nothing about a held recording
reaches the database until you pick it here — `take` transcribes it into a
source for /log like any voice note, `skip` archives the audio untranscribed
— or /log asks you about it. [intake] call_recordings = "auto" turns the
hold off. These are recordings; the call LOG (who called when) is the
observed tier's android_sms / ios_calls connectors, a separate thing.
"""

from __future__ import annotations

import typer

calls_app = typer.Typer(
    help="Call recordings in the audio inbox: held until you choose which to "
         "transcribe.", no_args_is_help=True)

_PICKS_HELP = ("Recordings to pick: numbers from `calls list`, or part of a "
               "file name that only one held recording contains")


def _mb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1_048_576:.1f} MB"


@calls_app.command("list")
def list_held(as_json: bool = typer.Option(False, "--json")) -> None:
    """The call recordings waiting for your pick, oldest first."""
    from ..cli import _cfg, _echo_json
    from .. import transcribe as tr
    cfg = _cfg()
    held = tr.held_call_info(cfg)
    if as_json:
        _echo_json(held)
        return
    if tr.call_mode(cfg) == "auto":
        typer.echo("[intake] call_recordings = \"auto\": call recordings are "
                   "transcribed like any note, so none are held")
        return
    if not held:
        typer.echo("no call recordings held — none in the inbox")
        return
    typer.echo(f"{len(held)} call recording(s) held, not transcribed:")
    for c in held:
        who = c["who"] or "(no name in the file)"
        typer.echo(f"  {c['n']:>2}. {c['local_time']}  {who:<24} {_mb(c['bytes']):>8}  "
                   f"{c['file']}  (journal day {c['journal_date']})")
    typer.echo("take one: lifecrm calls take <n> (transcribed into a source for "
               "/log) · skip one: lifecrm calls skip <n> (archived, untranscribed)")


@calls_app.command("take")
def take(picks: list[str] = typer.Argument(None, help=_PICKS_HELP),
         all_: bool = typer.Option(False, "--all", help="Every held recording"),
         as_json: bool = typer.Option(False, "--json")) -> None:
    """Transcribe the picked recordings: each becomes a source for its
    journal day, extracted and reviewed through /log like any note."""
    from ..cli import _cfg, _conn, _echo_json, _fail, _needs_attention
    from .. import transcribe as tr
    from ..domain.util import DomainError
    cfg = _cfg()
    try:
        chosen = tr.resolve_held(cfg, picks or [], all_)
    except DomainError as e:
        _fail(str(e))
    if not chosen:
        typer.echo("no call recordings held — nothing to take")
        return
    conn = _conn(cfg)
    try:
        results = tr.transcribe_inbox(conn, cfg, files=chosen)
    finally:
        conn.close()
    if as_json:
        _echo_json(results)
        _needs_attention(results)
        return
    for r in results:
        for w in r.get("warnings", []):
            typer.echo(w, err=True)
        if r["status"] == "transcribed":
            typer.echo(f"{r['file']}: source id={r['source_id']} ({r['journal_date']}, "
                       f"{r['duration_sec']}s, {r['segments']} segments) — /log "
                       f"extracts it with that day's notes")
        else:
            typer.echo(f"{r['file']}: {r['status']}")
    _needs_attention(results)


@calls_app.command("skip")
def skip(picks: list[str] = typer.Argument(None, help=_PICKS_HELP),
         all_: bool = typer.Option(False, "--all", help="Every held recording"),
         as_json: bool = typer.Option(False, "--json")) -> None:
    """Set the picked recordings aside: moved to the archive untranscribed,
    nothing written to the database. `lifecrm transcribe --file <archived
    path>` still takes one later."""
    from ..cli import _cfg, _echo_json, _fail
    from .. import transcribe as tr
    from ..domain.util import DomainError
    cfg = _cfg()
    try:
        chosen = tr.resolve_held(cfg, picks or [], all_)
        results = tr.skip_calls(cfg, chosen)
    except (DomainError, OSError) as e:
        _fail(str(e))
    if as_json:
        _echo_json(results)
        return
    if not results:
        typer.echo("no call recordings held — nothing to skip")
    for r in results:
        typer.echo(f"{r['file']}: skipped — archived untranscribed to {r['archived_to']}")


def register(app: typer.Typer) -> None:
    app.add_typer(calls_app, name="calls")

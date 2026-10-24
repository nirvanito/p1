"""`lifecrm demo` — a complete, synthetic second brain to try everything on.

Builds a throwaway instance (default: demo/<combo>/ in the project, which git
ignores) for one of the two supported setups — `android` (Windows laptop +
Samsung phone) or `iphone` (MacBook + iPhone) — by rendering one fictional
life in each app's real export format and running every real import over it.
Nothing touches the project's own data directory.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command("demo")
    def demo(
        combo: str = typer.Option(
            "android", "--combo",
            help="android (Windows laptop + Samsung) | iphone (MacBook + iPhone) | both"),
        directory: str = typer.Option(
            None, "--dir", help="Where to build it (default: demo/<combo>/ in the project)"),
        anchor: str = typer.Option(
            None, "--anchor",
            help="The demo's 'today', YYYY-MM-DD (default: today's journal date "
                 "in the demo's timezone — before 05:00 that is still yesterday)"),
        force: bool = typer.Option(False, "--force", help="Rebuild an existing demo"),
        with_audio: bool = typer.Option(
            False, "--with-audio",
            help="Also speak one note with the OS voice and transcribe it with "
                 "local whisper (needs the whisper model; slower)"),
        no_ask: bool = typer.Option(False, "--no-ask", help="Skip the sample questions"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Build a demo instance from synthetic data, through the real pipeline."""
        from ..cli import _fail
        from ..demo import persona
        from ..demo.build import (DemoBuilder, DemoError, check_dir_option, default_anchor,
                                  default_dir)

        combos = list(persona.COMBOS) if combo == "both" else [combo]
        if any(c not in persona.COMBOS for c in combos):
            _fail(f"--combo must be android, iphone or both (got {combo})")
        if directory and len(combos) > 1:
            _fail("--dir builds one demo; use it with --combo android or --combo iphone")
        if directory:
            try:
                check_dir_option(directory)
            except DemoError as e:
                _fail(str(e))
        try:
            # The rollover-aware journal date, not the calendar's: built at
            # 01:00, the demo would otherwise hold records its own `ask`
            # places tomorrow.
            day = date.fromisoformat(anchor) if anchor else default_anchor()
        except ValueError:
            _fail(f"--anchor must be YYYY-MM-DD (got {anchor})")

        echo = (lambda *_: None) if as_json else typer.echo
        summaries = []
        for c in combos:
            root = Path(directory) if directory else default_dir(c)
            try:
                builder = DemoBuilder(root, c, day, echo=echo, with_audio=with_audio,
                                      ask=not no_ask, force=force)
                summaries.append(builder.run())
            except DemoError as e:
                _fail(str(e))
            except Exception as e:                      # speech, a generator bug
                _fail(f"demo build failed: {e}")

        if as_json:
            typer.echo(json.dumps(summaries, indent=2, ensure_ascii=False, default=str))
            return
        for s in summaries:
            _print_summary(s)


def _print_summary(s: dict) -> None:
    from ..demo import persona
    dev = persona.DEVICE[s["combo"]]
    root = s["root"]
    doctor = s.get("doctor") or {}
    typer.echo("")
    typer.echo(f"=== demo ready: {dev['computer']} + {dev['phone']} ===")
    typer.echo(f"root:     {root}")
    typer.echo(f"journal:  {s['journal']['days']} days logged, "
               f"{s['journal']['entities']} people/places/things")
    typer.echo(f"imported: {', '.join(sorted(s['imported'])) or 'nothing'}")
    typer.echo(f"handles:  {s['handles']['linked']} linked, "
               f"{s['handles']['left_for_review']} left for you to review")
    if isinstance(doctor, dict):
        typer.echo(f"doctor:   {doctor.get('failures', '?')} failures, "
                   f"{doctor.get('warnings', '?')} warnings")
    for line in s.get("skipped", []):
        typer.echo(f"skipped:  {line}")
    if s.get("answers"):
        typer.echo("")
        typer.echo("sample questions (deterministic answers, every fact cited):")
        for a in s["answers"]:
            first = (a.get("answer") or "").strip().splitlines()[:1]
            typer.echo(f"  Q: {a['question']}")
            typer.echo(f"     {first[0][:160] if first else '(no answer)'}")
    r = f'--root "{root}"'
    typer.echo("")
    typer.echo("try it:")
    typer.echo(f"  python -m lifecrm {r} serve            # the web UI (Ask, Today, People...)")
    typer.echo(f"  python -m lifecrm {r} dashboard        # charts")
    typer.echo(f'  python -m lifecrm {r} ask "where is Jake from"')
    typer.echo(f"  python -m lifecrm {r} status")
    typer.echo(f"  python -m lifecrm {r} handles list --suggest")
    typer.echo(f"  python -m lifecrm {r} review --lens health")
    typer.echo(f"  python -m lifecrm {r} obsidian open    # the same data in Obsidian "
               f"(vault: {Path(root) / 'vault'})")

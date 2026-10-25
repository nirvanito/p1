"""`lifecrm features` — check and uncheck the read-side features.

The list and its switches are lifecrm/features.py; this module owns the
flags. `check` and `uncheck` take keys, their numbers from the list, or
--all; `pick` is the same list in the terminal, toggled by number. Every
write goes through features.save, which changes only [features] in
config.toml and refuses rather than touch anything else. docs/features.md.
"""

from __future__ import annotations

import sys

import typer

features_app = typer.Typer(
    help="Check and uncheck the read-side features (on this day, meeting prep, places, "
         "subscriptions, ...): the features table in config.toml.",
    invoke_without_command=True)


def register(app: typer.Typer) -> None:
    app.add_typer(features_app, name="features")


def _show(as_json: bool) -> None:
    from .. import features as mod
    from ..cli import _cfg, _echo_json
    data = mod.listing(_cfg())
    if as_json:
        _echo_json(data)
        return
    for line in mod.render_lines(data):
        typer.echo(line)


@features_app.callback()
def features(ctx: typer.Context,
             as_json: bool = typer.Option(False, "--json", help="Print the list as JSON")
             ) -> None:
    """List every feature as a checklist (default when no sub-command is given)."""
    if ctx.invoked_subcommand is not None:
        return
    _show(as_json)


@features_app.command("list")
def list_features(as_json: bool = typer.Option(False, "--json")) -> None:
    """List every feature as a checklist: x checked, blank unchecked."""
    _show(as_json)


def _set(names: list[str] | None, on: bool, every: bool) -> None:
    from .. import features as mod
    from ..cli import _cfg, _fail
    from ..domain.util import DomainError
    verb = "check" if on else "uncheck"
    if every and names:
        _fail(f"give feature keys or --all, not both")
    if not every and not names:
        _fail(f"{verb} what? give a key or its number (python -m lifecrm features "
              f"lists them), or --all")
    cfg = _cfg()
    try:
        keys = list(mod.KEYS) if every else mod.resolve(names)
        current = mod.listing(cfg)
        before = {x["key"]: x["on"] for x in current["features"]}
        path = mod.save(cfg, {k: on for k in keys})
    except DomainError as e:
        _fail(str(e))
    changed = [k for k in keys if before.get(k) != on]
    word = "checked" if on else "unchecked"
    if changed:
        typer.echo(f"{word}: {', '.join(changed)} ({path})")
    else:
        typer.echo(f"already {word}: {', '.join(keys)}")
    _show(False)


@features_app.command("check")
def check(names: list[str] = typer.Argument(None, metavar="KEY|N...",
                                            help="Feature keys or their numbers"),
          every: bool = typer.Option(False, "--all", help="Check every feature")) -> None:
    """Turn features on: <key> = true in the features table of config.toml."""
    _set(names, True, every)


@features_app.command("uncheck")
def uncheck(names: list[str] = typer.Argument(None, metavar="KEY|N...",
                                              help="Feature keys or their numbers"),
            every: bool = typer.Option(False, "--all", help="Uncheck every feature")) -> None:
    """Turn features off: <key> = false in the features table. Nothing is deleted — a
    feature only reads, so checking it again shows it exactly as before."""
    _set(names, False, every)


@features_app.command("on", hidden=True)
def on(names: list[str] = typer.Argument(None), every: bool = typer.Option(False, "--all")
       ) -> None:
    """Same as check."""
    _set(names, True, every)


@features_app.command("off", hidden=True)
def off(names: list[str] = typer.Argument(None), every: bool = typer.Option(False, "--all")
        ) -> None:
    """Same as uncheck."""
    _set(names, False, every)


PICK_HELP = ("type numbers to toggle (e.g. 2 5 7), 'a' checks all, 'n' unchecks all, "
             "Enter saves, 'q' quits without saving")


@features_app.command("pick")
def pick() -> None:
    """The checklist in the terminal: toggle by number, then save."""
    from .. import features as mod
    from ..cli import _cfg, _fail
    from ..domain.util import DomainError
    cfg = _cfg()
    data = mod.listing(cfg)
    if data.get("error"):
        _fail(data["error"])
    states = {x["key"]: x["on"] for x in data["features"]}
    original = dict(states)
    while True:
        for i, f in enumerate(mod.FEATURES, start=1):
            typer.echo(f"  [{'x' if states[f.key] else ' '}] {i:>2} {f.key} — {f.title}")
        try:
            raw = input(f"{PICK_HELP}\n> " if sys.stdin.isatty() else "> ")
        except EOFError:
            raw = ""
        text = raw.strip().lower()
        if text in ("q", "quit", "exit"):
            typer.echo("nothing saved")
            return
        if not text:
            break
        if text in ("a", "all"):
            states = {k: True for k in states}
            continue
        if text in ("n", "none"):
            states = {k: False for k in states}
            continue
        try:
            for key in mod.resolve(text.replace(",", " ").split()):
                states[key] = not states[key]
        except DomainError as e:
            typer.echo(f"  {e}")
    changes = {k: v for k, v in states.items() if v != original[k]}
    if not changes:
        typer.echo("no change")
        return
    try:
        path = mod.save(cfg, changes)
    except DomainError as e:
        _fail(str(e))
    on_ = [k for k, v in changes.items() if v]
    off_ = [k for k, v in changes.items() if not v]
    typer.echo("saved to " + str(path) + ": "
               + "; ".join(x for x in (f"checked {', '.join(on_)}" if on_ else "",
                                       f"unchecked {', '.join(off_)}" if off_ else "") if x))

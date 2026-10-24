"""`lifecrm obsidian refresh | open` — the Obsidian vault as a main GUI.

The vault is export.py's read-only mirror plus the start pages
lifecrm/obsidian.py renders; this module is only the commands. With
`[obsidian] vault` set in config.toml every CLI command that changes the
database refreshes it by itself (cli._arm_vault_refresh), so `refresh` is for
the first time and for after `[obsidian] auto_refresh = false`.
docs/obsidian.md has the setup for both combos.
"""

from __future__ import annotations

import typer

obsidian_app = typer.Typer(
    help="The Obsidian vault as a main GUI: a read-only mirror, kept current.",
    no_args_is_help=True)

_VAULT_HELP = ("The vault folder (default: [obsidian] vault in config.toml)")


def _vault(cfg, given: str | None):
    from ..cli import _fail
    from .. import obsidian
    from ..domain.util import DomainError
    try:
        vault = obsidian.vault_dir(cfg, given)
    except DomainError as e:
        _fail(str(e))
    if vault is None:
        _fail("no vault folder: pass --vault, or set it once in config.toml — "
              "[obsidian] vault = \"~/OneDrive/LifeCRM vault\" (docs/obsidian.md)")
    return vault


@obsidian_app.command("refresh")
def refresh(vault: str = typer.Option(None, "--vault", help=_VAULT_HELP),
            dry_run: bool = typer.Option(False, "--dry-run",
                                         help="List what would change, write nothing")
            ) -> None:
    """Bring the vault in line with the database — the same one-way export as
    `export --format obsidian`, start pages included."""
    from ..cli import _cfg, _fail
    from .. import export as export_mod, obsidian
    from ..domain.util import DomainError
    cfg = _cfg()
    path = _vault(cfg, vault)
    try:
        result = obsidian.refresh(cfg, path, dry_run=dry_run)
    except DomainError as e:
        _fail(str(e))
    for line in export_mod.vault_report_lines(result, path):
        typer.echo(line)


@obsidian_app.command("open")
def open_vault(vault: str = typer.Option(None, "--vault", help=_VAULT_HELP),
               no_refresh: bool = typer.Option(False, "--no-refresh",
                                               help="Open it as it is")) -> None:
    """Refresh the vault, then open Obsidian on its Home note."""
    from ..cli import _cfg, _fail
    from .. import obsidian
    from ..domain.util import DomainError
    cfg = _cfg()
    path = _vault(cfg, vault)
    try:
        if not no_refresh:
            result = obsidian.refresh(cfg, path)
            n = obsidian.changed(result)
            typer.echo(f"vault {path}: {result['files']} generated note(s), "
                       f"{n} changed")
        for line in obsidian.open_vault(path):
            typer.echo(line)
    except DomainError as e:
        _fail(str(e))


def register(app: typer.Typer) -> None:
    app.add_typer(obsidian_app, name="obsidian")

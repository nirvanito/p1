"""`lifecrm open` — open the main GUI, whichever one you chose.

Two front ends read the same database: the web UI (`lifecrm serve`) and the
Obsidian vault (`lifecrm obsidian open`). `[gui] main` in config.toml says
which one is yours ("web", the default, or "obsidian"); --gui picks for this
once. Neither is a write path: logging a day stays /log, changes stay CLI
commands.
"""

from __future__ import annotations

import typer


def open_gui(gui: str = typer.Option(None, "--gui",
                                     help="web | obsidian (default: [gui] main, else web)"),
             vault: str = typer.Option(None, "--vault",
                                       help="obsidian: the vault folder "
                                            "(default: [obsidian] vault)"),
             port: int = typer.Option(None, "--port",
                                      help="web: the port (default 8765, [web] port)")
             ) -> None:
    """Open the main GUI: the web UI in the browser (serving until Ctrl+C), or
    the Obsidian vault, refreshed first."""
    from ..cli import _cfg, _fail
    from .. import obsidian
    from ..domain.util import DomainError
    cfg = _cfg()
    try:
        choice = obsidian.main_gui(cfg, gui)
    except DomainError as e:
        _fail(str(e))
    if choice == "obsidian":
        from .obsidian import open_vault
        open_vault(vault=vault, no_refresh=False)
        return
    if vault:
        _fail("--vault is for --gui obsidian")
    from .web import serve
    serve(host=None, port=port, token=None, no_log=False, no_llm=False,
          open_browser=True, verbose=False)


def register(app: typer.Typer) -> None:
    app.command("open")(open_gui)

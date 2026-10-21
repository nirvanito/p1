"""CLI plug-ins: one module per feature that only adds commands.

A module here defines `register(app: typer.Typer) -> None` and attaches its
commands (or a sub-typer) to the root app. `lifecrm/cli.py` discovers and
registers every module at import time, so adding a command group never edits
cli.py. Modules may import cli's helpers lazily inside their command bodies
(`from ..cli import _cfg, _conn, _fail`), or at module level — cli has
defined them all before it registers plug-ins.
"""

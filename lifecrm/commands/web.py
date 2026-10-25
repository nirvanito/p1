"""`lifecrm serve` — the read-only web UI for a laptop browser or a phone.

The server lives in lifecrm/web/; this module is only the command: options,
the migration gate, the printed URLs and the LAN warning. docs/web.md has the
phone setup for both combos (Windows + Samsung, Mac + iPhone).
"""

from __future__ import annotations

import secrets

import typer


def serve(
    host: str = typer.Option(
        None, "--host",
        help="127.0.0.1 (default, this computer only) or 0.0.0.0 to reach it "
             "from your phone on the same Wi-Fi"),
    port: int = typer.Option(None, "--port", help="Port (default 8765, [web] port)"),
    token: str = typer.Option(
        None, "--token",
        help="Require ?k=<token> on every request (a cookie remembers it); "
             "'random' makes one up"),
    no_log: bool = typer.Option(False, "--no-log",
                                help="Do not record questions in recall_log"),
    no_llm: bool = typer.Option(False, "--no-llm",
                                help="Deterministic answers only, never a local model"),
    open_browser: bool = typer.Option(False, "--open",
                                      help="Open the page in this computer's browser"),
    verbose: bool = typer.Option(False, "--verbose",
                                 help="Log each request (paths only — never "
                                      "questions or the token)"),
) -> None:
    """Serve the read-only web UI: ask with citations, today, people, loops,
    sources, health — on this computer or your phone.

    It reads a snapshot of the database (data/web/lifecrm.db), refreshed
    automatically when the live file changes, so the live DB is never held
    open. The database gets nothing but, unless --no-log, the question log
    that `lifecrm ask` also keeps; with --token, the Sources page can also
    check and uncheck features, which rewrites only [features] in
    config.toml. There is no capture here: logging a day stays the reviewed
    /log path. Ctrl+C to stop.
    """
    from ..cli import _cfg, _conn, _fail
    from ..web import server as web_server

    cfg = _cfg()
    web = web_server.web_settings(cfg)
    host = host or str(web.get("host", "127.0.0.1"))
    port = port if port is not None else int(web.get("port", web_server.DEFAULT_PORT))
    token = token if token is not None else (web.get("token") or None)
    if token == "random":
        token = secrets.token_urlsafe(9)
    log_questions = False if no_log else web_server.flag(web.get("log_questions"), True)
    use_llm = False if no_llm else web_server.flag(web.get("use_llm"), True)

    _conn(cfg).close()            # write-capable connect: the migration gate
    try:
        server = web_server.create_server(
            cfg, host=host, port=port, token=token, log_questions=log_questions,
            use_llm=use_llm, verbose=verbose)
    except web_server.SnapshotError as e:
        _fail(str(e))
    except OSError as e:
        _fail(f"cannot listen on {host}:{port} ({e.strerror or e}) — is another "
              f"server using the port? pass --port N")
    actual = server.server_address[1]
    for line in web_server.banner(cfg, host, actual, token, log_questions):
        typer.echo(line)
    if open_browser:
        import webbrowser
        webbrowser.open(web_server.local_url(actual, token))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        server.server_close()


def register(app: typer.Typer) -> None:
    app.command("serve")(serve)

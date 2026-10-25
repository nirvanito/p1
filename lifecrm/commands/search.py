"""`lifecrm search`, `links`, `conversation`, `weekdays` — browse what the
records hold (lifecrm/search.py, links.py, web/api.conversation,
weekday.py). Read-only.
"""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    app.command("search")(search)
    app.command("links")(links)
    app.command("conversation")(conversation)
    app.command("weekdays")(weekdays)


def _person(conn, name: str) -> int:
    """An entity id from a name, an alias or an id; one-line failure when it
    names nobody or more than one person."""
    from ..cli import _fail
    from ..domain import entities
    if name.isdigit():
        return int(name)
    found = entities.match(conn, name, "person")
    matches = found.get("matches") or []
    if found.get("status") in ("exact", "alias") and len(matches) == 1:
        return matches[0]["id"]
    if not matches:
        _fail(f"no person called '{name}' — python -m lifecrm entities list --type person")
    if len(matches) > 1 and found.get("status") not in ("exact", "alias"):
        names = ", ".join(f"{m['name']} (id {m['id']})" for m in matches[:5])
        _fail(f"'{name}' could be {names} — give the id")
    return matches[0]["id"]


def search(query: list[str] = typer.Argument(..., metavar="WORDS..."),
           tier: str = typer.Option(None, "--tier", help="confirmed | observed"),
           stream: str = typer.Option(None, "--stream",
                                      help="entry | passage | an observation stream"),
           person: str = typer.Option(None, "--person", help="A name or an entity id"),
           year: str = typer.Option(None, "--year", metavar="YYYY"),
           limit: int = typer.Option(30, "--limit", min=1, max=500),
           as_json: bool = typer.Option(False, "--json")) -> None:
    """Every record the words match — journal entries, transcripts and
    imports — newest first, with counts by tier, kind, year and person."""
    from .. import search as search_mod
    from ..cli import _cfg, _echo_json, _fail
    from .memory import _read
    from .places import need
    cfg = _cfg()
    need(cfg, "search")
    q = " ".join(query)

    def run(conn):
        pid = _person(conn, person) if person else None
        return search_mod.search(conn, cfg, q, tier=tier, stream=stream, person=pid,
                                 year=year, limit=limit)

    try:
        data = _read(cfg, run)
    except ValueError as e:
        _fail(str(e))
    if as_json:
        _echo_json(data)
        return
    for line in search_mod.render_lines(data):
        typer.echo(line)


def links(target: str = typer.Argument(None, metavar="URL|SITE",
                                       help="A link or a site, e.g. nytimes.com"),
          sender: str = typer.Option(None, "--from", help="The links a person sent you"),
          start: str = typer.Option(None, "--start", metavar="YYYY-MM-DD"),
          end: str = typer.Option(None, "--end", metavar="YYYY-MM-DD"),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Where a link or a site turned up — texts, email, notes, chats, browser
    history, the journal — and who sent it; or every link a person sent."""
    from .. import links as links_mod
    from ..cli import _cfg, _echo_json, _fail
    from .memory import _read
    from .places import need
    cfg = _cfg()
    need(cfg, "link_memory")
    if bool(target) == bool(sender):
        _fail("give a link or a site, or --from <person> — one of them")
    if sender:
        def run(conn):
            return links_mod.from_person(conn, cfg, _person(conn, sender), limit=100)
        found = _read(cfg, run)
        if as_json:
            _echo_json(found)
            return
        typer.echo(f"{len(found)} link(s) {sender} sent you (newest first, observed):")
        for x in found:
            typer.echo(f"  {x['date']} {x['stream']}: {x['link']} · {x['uuid']}")
        return
    data = _read(cfg, lambda conn: links_mod.sightings(conn, cfg, target, start=start,
                                                        end=end))
    if as_json:
        _echo_json(data)
        return
    for line in links_mod.render_lines(data):
        typer.echo(line)


def conversation(person: str = typer.Argument(..., metavar="PERSON",
                                              help="A name, an alias or an entity id"),
                 limit: int = typer.Option(40, "--limit", min=1, max=300),
                 as_json: bool = typer.Option(False, "--json")) -> None:
    """One thread with a person across every app: the texts, chats, emails
    and calls between the two of you, the newest `--limit`, oldest first."""
    from ..cli import _cfg, _echo_json
    from ..web import api
    from .memory import _read
    from .places import need
    cfg = _cfg()
    need(cfg, "conversations")

    def run(conn):
        return api.conversation(conn, cfg, _person(conn, person), limit=limit)

    data = _read(cfg, run)
    if as_json:
        _echo_json(data)
        return
    typer.echo(f"Conversation with {data['name']} ({', '.join(data['streams']) or 'nothing'}"
               f"; observed, oldest first):")
    for o in data["items"]:
        text = " ".join((o.get("text") or o.get("title") or "").split())
        typer.echo(f"  {o['local_time'] or o['date']} {o['who'] or ''} [{o['stream']}]: {text}")
    if data["older"]:
        typer.echo(f"  (older messages before {data['older']}: --limit {limit * 2})")


def weekdays(days: int = typer.Option(90, "--days", min=14, max=3660,
                                      help="The N days ending today"),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Each series averaged by day of the week, over the days it knows."""
    from .. import weekday as wk
    from ..cli import _cfg, _echo_json
    from .memory import _day, _read
    from .places import need
    cfg = _cfg()
    need(cfg, "weekday_profile")
    start, end = wk.window(_day(cfg, None), days)
    items = _read(cfg, lambda conn: wk.profiles(conn, cfg, start, end))
    if as_json:
        _echo_json([{k: v for k, v in p.items() if k != "refs"} for p in items])
        return
    for line in wk.render_lines(items):
        typer.echo(line)

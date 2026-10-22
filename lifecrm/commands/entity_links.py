"""`lifecrm entities link ...` — relationships between entities.

A plug-in like every module here, with one difference: it attaches its group
under the existing `entities` group rather than the root app. Relationships
are an entity concern, and the root `lifecrm link` already means typed links
between ENTRIES; two meanings of one word at the same level would be a trap.

Every write routes through lifecrm/domain/entity_links.py. Removing a
relationship retracts it (kept, audited, restorable) — nothing here deletes.
"""

from __future__ import annotations

import typer

link_app = typer.Typer(
    help="Relationships between entities: Jake works_at Acme, Jake from El Paso.",
    no_args_is_help=True)


def register(app: typer.Typer) -> None:
    from ..cli import entities_app
    entities_app.add_typer(link_app, name="link")


def _line(link: dict) -> str:
    extra = []
    if link.get("valid_from") or link.get("valid_to"):
        extra.append(f"{link.get('valid_from') or '?'} – {link.get('valid_to') or 'now'}")
    if link.get("note"):
        extra.append(link["note"])
    if link.get("status") == "retracted":
        extra.append("retracted")
    return (f"{link['uuid'][:8]}  {link['text']}"
            + (f"  ({'; '.join(extra)})" if extra else ""))


@link_app.command("add")
def link_add(
    from_id: int = typer.Argument(..., help="Entity id the sentence starts with"),
    to_id: int = typer.Argument(..., help="Entity id it ends with"),
    link_type: str = typer.Option(..., "--type",
                                  help="knows | friend_of | partner_of | family_of | "
                                       "parent_of | child_of | sibling_of | "
                                       "colleague_of | manages | works_at | member_of | "
                                       "lives_in | from | located_in | part_of | "
                                       "relates (see: entities link types)"),
    note: str = typer.Option(None, "--note"),
    since: str = typer.Option(None, "--since", help="When it began (YYYY-MM-DD)"),
    until: str = typer.Option(None, "--until", help="When it ended (YYYY-MM-DD)"),
    entry: str = typer.Option(None, "--entry", help="Cite the entry that states it"),
    observation: str = typer.Option(None, "--observation",
                                    help="Cite the observation that states it "
                                         "(a contact card, a message)"),
) -> None:
    """Relate two entities. Direction reads as a sentence, FROM type TO:
    Jake works_at Acme, Jake from El Paso, Linda parent_of Alex. Symmetric
    types (knows, friend_of, partner_of, ...) read either way and are stored
    once."""
    from ..cli import _cfg, _commit_or_rollback, _conn
    from ..domain import entity_links
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(conn, lambda: entity_links.add(
        conn, from_id, to_id, link_type, note=note, valid_from=since,
        valid_to=until, entry_uuid=entry, observation_uuid=observation))
    if result["created"]:
        typer.echo(f"linked: {result['text']}  ({result['uuid']})")
    else:
        typer.echo(f"already known: {result['text']}  ({result['uuid']})")


@link_app.command("list")
def link_list(
    entity_id: int = typer.Argument(None, help="Entity id (omit for every relationship)"),
    link_type: str = typer.Option(None, "--type"),
    include_retracted: bool = typer.Option(False, "--all",
                                           help="Include retracted relationships"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Relationships of one entity (both directions), or all of them."""
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..domain import entity_links
    from ..domain.util import DomainError
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        if entity_id is None:
            rows = entity_links.list_links(conn, link_type=link_type,
                                           include_retracted=include_retracted)
        else:
            rows = entity_links.for_entity(
                conn, entity_id, types=(link_type,) if link_type else None,
                include_retracted=include_retracted)
    except DomainError as e:
        _fail(str(e))
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
        return
    for r in rows:
        typer.echo(_line(r))
    if not rows:
        typer.echo("(no relationships)")


@link_app.command("rm")
def link_rm(link_uuid: str = typer.Argument(..., help="Relationship uuid (or 8+ char prefix)"),
            reason: str = typer.Option(None, "--reason", help="Why, for the audit log")) -> None:
    """Retract a relationship. Not destructive: the row stays, audited, and
    `entities link restore` brings it back."""
    from ..cli import _cfg, _commit_or_rollback, _conn
    from ..domain import entity_links
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(
        conn, lambda: entity_links.retract(conn, link_uuid, reason=reason))
    typer.echo(f"retracted: {result['text']}  ({result['uuid']})")


@link_app.command("restore")
def link_restore(link_uuid: str = typer.Argument(..., help="Relationship uuid (or 8+ char prefix)")) -> None:
    """Bring back a retracted relationship (both ends must be live entities)."""
    from ..cli import _cfg, _commit_or_rollback, _conn
    from ..domain import entity_links
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(conn, lambda: entity_links.restore(conn, link_uuid))
    typer.echo(f"restored: {result['text']}  ({result['uuid']})")


@link_app.command("types")
def link_types(as_json: bool = typer.Option(False, "--json")) -> None:
    """Relationship types and how each reads."""
    from ..cli import _cfg, _conn, _echo_json
    from ..domain import entity_links
    cfg = _cfg()
    conn = _conn(cfg, readonly=True)
    try:
        rows = entity_links.link_types(conn)
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
        return
    for r in rows:
        shape = ("symmetric" if r["symmetric"]
                 else f"inverse: {r['inverse']}" if r["inverse"] else "directed")
        typer.echo(f"{r['name']:<13} {r['description']}  [{shape}]")


@link_app.command("type-add")
def link_type_add(
    name: str = typer.Argument(..., help="lowercase_snake_case, e.g. reports_to"),
    description: str = typer.Option(..., "--description",
                                    help="How 'FROM type TO' reads, e.g. "
                                         "'FROM reports to TO'"),
    symmetric: bool = typer.Option(False, "--symmetric",
                                   help="Reads the same both ways (stored once)"),
    inverse: str = typer.Option(None, "--inverse",
                                help="An existing directed type stating the same "
                                     "fact from the other end (manages)"),
) -> None:
    """Add a relationship type — a row, never a migration."""
    from ..cli import _cfg, _commit_or_rollback, _conn
    from ..domain import entity_links
    cfg = _cfg()
    conn = _conn(cfg)
    result = _commit_or_rollback(conn, lambda: entity_links.add_type(
        conn, name, description, symmetric=symmetric, inverse=inverse))
    typer.echo(f"relationship type '{result['name']}' added")

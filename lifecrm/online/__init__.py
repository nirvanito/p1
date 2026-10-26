"""Online sources: services the daily collect job fetches from by itself, so
nothing has to be exported by hand (docs/online.md).

Every other source reaches this computer on its own — a phone app's backup
in OneDrive, the browser's own history file. These live only on a service's
servers, so they are fetched: each morning, before collect reads them,
the service's new data is downloaded under `data/fetched/<service>/` in a
format an existing connector already reads (an .ics folder for the calendar
connector, .eml files for email, a vCard for contacts), and from there it
goes through the very same plan and holds as any other source — a source's
first import still waits for the owner's yes. Nothing here writes the
database.

A service module defines

    SERVICE   = "google_calendar"     # its name in `lifecrm connect`
    CONNECTOR = "calendar"            # the connector that reads what it fetches
    TITLE     = "Google Calendar"
    def steps() -> list[str]          # the one-time setup, in words
    def connect(cfg, args, ask) -> dict   # what fetch needs, from the owner
    def fetch(cfg, secret, dest) -> Path  # download into dest, return the target
    def settle(cfg, secret, target)       # optional: what target held is imported

A service that fetches its whole state each time (a calendar link) needs no
`settle`. One that fetches only what is new (mail) keeps it pending until a
committed import says it is in — so a first import waiting for the owner's
yes still finds everything — and `settle` then moves it aside.

`connect` returns the values to keep (a private link, an app password, a
refresh token); they are stored in `data/secrets.json`, never in
config.toml and never printed. `ask(prompt, hide)` is how a module asks the
owner (the CLI passes typer.prompt): the owner types a password into their
own terminal, nothing else ever does. Fetching is the only network use of
the daily job; `places fetch` is the other one in the project.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from ..config import Config
from ..domain.util import DomainError
from . import secrets


def registry() -> dict:
    """{SERVICE: module} for every module here that defines SERVICE and fetch()."""
    from .. import online as pkg
    found = {}
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if info.name.startswith("_") or info.name == "secrets":
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        if isinstance(getattr(mod, "SERVICE", None), str) and callable(
                getattr(mod, "fetch", None)):
            found[mod.SERVICE] = mod
    return found


def get(service: str):
    reg = registry()
    if service not in reg:
        raise DomainError(f"unknown online service '{service}' — one of: "
                          f"{', '.join(sorted(reg)) or '(none)'}")
    return reg[service]


def dest_dir(cfg: Config, service: str) -> Path:
    return cfg.data_dir / "fetched" / service


def connected(cfg: Config) -> dict:
    """{SERVICE: module} for the services the owner connected."""
    kept = secrets.load(cfg)
    return {s: m for s, m in registry().items() if kept.get(s)}


def for_connector(cfg: Config, connector: str):
    """The connected service feeding `connector`, or None."""
    for mod in connected(cfg).values():
        if mod.CONNECTOR == connector:
            return mod
    return None


def feeds() -> set[str]:
    """Every connector some online service can feed."""
    return {m.CONNECTOR for m in registry().values()}


def fetch(cfg: Config, mod) -> Path:
    """Download the service's new data; the path its connector reads."""
    secret = secrets.load(cfg).get(mod.SERVICE)
    if not secret:
        raise DomainError(f"{mod.TITLE} is not connected — run: python -m lifecrm "
                          f"connect {mod.SERVICE}")
    dest = dest_dir(cfg, mod.SERVICE)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        return mod.fetch(cfg, secret, dest)
    except DomainError:
        raise
    except OSError as e:          # network down, DNS, timeout, refused
        raise DomainError(f"could not reach {mod.TITLE} ({e}) — nothing fetched; the "
                          f"next run tries again") from e


def settle(cfg: Config, connector: str, target: Path) -> None:
    """Tell the service feeding `connector` that what it fetched into
    `target` is now imported (an optional `settle(cfg, secret, target)`:
    an incremental fetcher moves its pending files aside). Nothing to do
    for a service that fetches its whole state each time."""
    mod = for_connector(cfg, connector)
    if mod is None or not callable(getattr(mod, "settle", None)):
        return
    mod.settle(cfg, secrets.load(cfg).get(mod.SERVICE) or {}, target)


def connect(cfg: Config, service: str, args: list[str], ask) -> dict:
    """Run the service's own setup and keep what it returns."""
    mod = get(service)
    values = mod.connect(cfg, list(args), ask)
    if not isinstance(values, dict) or not values:
        raise DomainError(f"{mod.TITLE}: nothing to keep — not connected")
    secrets.put(cfg, service, values)
    return {"service": service, "title": mod.TITLE, "connector": mod.CONNECTOR}


def disconnect(cfg: Config, service: str) -> bool:
    get(service)
    return secrets.forget(cfg, service)


def status(cfg: Config) -> list[dict]:
    kept = secrets.load(cfg)
    return [{"service": s, "title": m.TITLE, "connector": m.CONNECTOR,
             "connected": bool(kept.get(s)), "steps": m.steps()}
            for s, m in sorted(registry().items())]

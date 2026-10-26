"""What the online services need to fetch — a private calendar link, an app
password, a refresh token — kept in `data/secrets.json` (data/ is private
and never committed; config.toml stays shareable). Written whole and
atomically, readable only by this user where the system allows it, and
never printed: commands say *that* a service is connected, never with what.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import Config
from ..domain.util import DomainError

FILE = "secrets.json"


def path(cfg: Config) -> Path:
    return cfg.data_dir / FILE


def load(cfg: Config) -> dict:
    p = path(cfg)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DomainError(f"{p} is not valid JSON ({e.msg}, line {e.lineno}) — it is "
                          f"written by `lifecrm connect`; reconnect the service") from e
    return data if isinstance(data, dict) else {}


def _write(cfg: Config, data: dict) -> None:
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)         # owner-only on a Mac; Windows keeps its ACLs
    except OSError:
        pass
    os.replace(tmp, p)


def put(cfg: Config, service: str, values: dict) -> None:
    data = load(cfg)
    data[service] = values
    _write(cfg, data)


def update(cfg: Config, service: str, **values) -> None:
    """Merge into a service's entry — a token refreshed, a cursor moved."""
    data = load(cfg)
    data[service] = {**(data.get(service) or {}), **values}
    _write(cfg, data)


def forget(cfg: Config, service: str) -> bool:
    data = load(cfg)
    if service not in data:
        return False
    del data[service]
    _write(cfg, data)
    return True

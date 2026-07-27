"""Shared helpers for the domain layer."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone


class DomainError(Exception):
    """Raised for any invalid mutation; messages are single-line and precise."""


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uuid() -> str:
    return str(uuid.uuid4())


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "item"


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_iso_date(value: str) -> bool:
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def is_iso_datetime(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False

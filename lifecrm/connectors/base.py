"""Connector contract for the observed tier.

A connector is a **pure reader**. It opens a file (or calls an API), returns
normalized records, and never touches the database. Everything downstream —
dedupe, classification, transactional apply, retraction — lives once in
`lifecrm/domain/observations.py` and is shared by every connector.

That split is the point. `health_connect.py` (F8) carries its own
build_plan/apply_plan; every connector after it would have copied them. Here a
new source of data means writing one `scan()` function and a report formatter.

A connector returns a `Scan`:

    connector    str                  registry name, e.g. 'ai_chat'
    account      str | None           which account, when the export identifies one
    source_file  str | None           the file it read
    source_hash  str | None           content hash; an unchanged export is a no-op
    records      list[dict]           observation dicts (see domain/observations)
    handles      list[dict]           {kind, value, display_name} seen in the export
    unmapped     list[dict]           what it saw and did not understand
    skipped      list[str]            what it deliberately dropped, with counts

`records` carry an optional `participants` list of {kind, value, role} naming
handles. Connectors emit **handles, never names** — a phone number or account
id, not "Jake". Resolution is a user decision made once per handle.

A scan that is its source's whole state may add `present_ids` and
`mirror_streams` (mirroring: what the source no longer holds is removed),
and `mirror_ages_out` when the source empties its oldest end by itself (a
keep-messages limit): then only what is missing from inside the span it
still holds counts as deleted. `coverage` declares days complete. A lookup
that finds several candidates and will not guess raises `Ambiguous`.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import Config
from ..domain import days
from ..domain.util import DomainError


class Scan(dict):
    """Plain dict with a fixed shape; see the module docstring."""


class Ambiguous(DomainError):
    """`default_target` found more than one candidate (two phones' backups,
    two camera folders) and never guesses between them: the owner names one."""


class SourceLocked(DomainError):
    """Another program holds the source database locked and SQLite's backup
    could not get a consistent copy: try again later."""


_SQLITE_BUSY, _SQLITE_LOCKED = 5, 6
LOCKED_TRIES = 6       # each try already waits out the connection's busy timeout


def sqlite_backup(source, dest, what) -> None:
    """`source.backup(dest)` that gives up instead of hanging. CPython retries
    a busy or locked source forever, sleeping between tries — with an Apple
    daemon holding the database that is a daily job that never ends. After
    LOCKED_TRIES busy steps this raises SourceLocked naming `what`."""
    busy = 0

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal busy
        if status in (_SQLITE_BUSY, _SQLITE_LOCKED):
            busy += 1
            if busy >= LOCKED_TRIES:
                raise SourceLocked(f"{what} stayed locked by another program — no "
                                   f"consistent copy could be made now; try again later "
                                   f"(the daily job does, tomorrow)")
    source.backup(dest, progress=progress)


@runtime_checkable
class Connector(Protocol):
    name: str
    streams: tuple[str, ...]

    def scan(self, target: Path, cfg: Config) -> Scan: ...
    def report_lines(self, scan: Scan, plan: dict) -> list[str]: ...


def empty_scan(connector: str, **kw) -> Scan:
    base = {"connector": connector, "account": None, "source_file": None,
            "source_hash": None, "records": [], "handles": [],
            "unmapped": [], "skipped": []}
    base.update(kw)
    return Scan(base)


# ---------------------------------------------------------------- time

def iso_utc(value) -> str | None:
    """Normalize a source timestamp to 'YYYY-MM-DDTHH:MM:SSZ'.

    Accepts epoch seconds (ChatGPT) or an ISO-8601 string with any precision
    and any offset (Claude). Sub-second precision is deliberately dropped: it
    is not meaningful here and keeping it would make two exports of the same
    message compare unequal.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def journal_date_for_iso(iso: str, cfg: Config) -> str:
    """Journal date for a UTC instant, honoring the configured timezone and
    rollover hour — the same rule sources and health records follow.

    Chat exports carry no per-record zone offset, so the configured home
    timezone is used. A conversation held while travelling therefore lands on
    the home-timezone day. That is a known, documented limitation, not a bug:
    the export simply does not say where you were.
    """
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return days.journal_date_for(dt, cfg.timezone, cfg.day_rollover_hour)


# ---------------------------------------------------------------- handles

def normalize_phone(raw: str) -> str:
    """One canonical spelling per phone number, so a number seen in texts, a
    contact card and a calendar invite is one handle, not three.

    '+' plus digits when the source says so. Otherwise North American numbers
    are recognised by shape — 10 digits whose area code starts 2-9, or 11
    digits starting with 1 — and given +1. Anything else stays as bare digits:
    a national-format number from another country cannot be completed without
    knowing the country, and guessing would merge two different people.
    """
    s = raw.strip()
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10 and digits[0] in "23456789":
        return "+1" + digits
    return digits


_PHONE_SHAPE = re.compile(r"^\+?[\d\s().\-]+$")


def handle_for(raw: str | None) -> tuple[str, str] | None:
    """Classify a raw address as a (kind, value) handle, or None if empty.

    Emails are lower-cased; phone-shaped strings are normalized. Anything else
    — a Business Chat urn, an alphanumeric sender id like 'AMAZON' — is still
    an identifier worth keeping (it can be linked or ignored), so it becomes
    an app_account handle rather than being dropped.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s[:2].lower() in ("e:", "p:") and len(s) > 2:      # account prefixes
        s = s[2:].strip()
    if not s:
        return None
    if "@" in s and not s.lower().startswith("urn:"):
        return ("email", s.lower())
    if _PHONE_SHAPE.match(s) and re.search(r"\d", s):
        return ("phone", normalize_phone(s))
    return ("app_account", f"messages:{s}")


# ---------------------------------------------------------------- mirroring

def removal_lines(plan: dict, why: str, label) -> list[str]:
    """Report lines for what a mirroring import would remove, or why it will
    remove nothing. `label(row)` names the thread a removed row belongs to."""
    lines = []
    if plan.get("mirror_note"):
        lines.append(f"  not mirroring deletions: {plan['mirror_note']}")
    if plan.get("aged_out"):
        lines.append(f"  kept: {plan['aged_out']} older than the oldest message the "
                     f"source still holds — aged out of it (a keep-messages limit), "
                     f"not deleted")
    removed = plan.get("removed") or []
    if not removed:
        return lines
    per_stream: dict[str, int] = {}
    per_thread: dict[str, int] = {}
    for row in removed:
        per_stream[row["stream"]] = per_stream.get(row["stream"], 0) + 1
        name = label(row) or "?"
        per_thread[name] = per_thread.get(name, 0) + 1
    top = sorted(per_thread.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    lines.append(f"  remove: {len(removed)} {why} — "
                 + ", ".join(f"{n} {s}" for s, n in sorted(per_stream.items()))
                 + " | from " + ", ".join(f"{t} ({n})" for t, n in top))
    return lines


# ---------------------------------------------------------------- files

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json_member(target: Path, *names: str) -> tuple[dict | list, str]:
    """Load the first of `names` found, from a .zip, a directory, or a direct
    path to the file itself. Returns (parsed, member_name).

    Raises DomainError naming what was actually present — an import that fails
    should say what it saw, not just that it failed.
    """
    if target.is_dir():
        for n in names:
            p = target / n
            if p.exists():
                return _load_json(p.read_bytes(), n), n
        listing = ", ".join(sorted(p.name for p in target.iterdir())[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} contains none of {', '.join(names)} (has: {listing})")

    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as zf:
            members = zf.namelist()
            for n in names:
                hit = next((m for m in members
                            if Path(m).name.lower() == n.lower()), None)
                if hit is not None:
                    return _load_json(zf.read(hit), hit), hit
            listing = ", ".join(members[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} contains none of {', '.join(names)} (members: {listing})")

    if target.name.lower() in {n.lower() for n in names} or \
            target.suffix.lower() == ".json":
        return _load_json(target.read_bytes(), target.name), target.name

    raise DomainError(f"{target.name} is not a .zip, a directory, or a .json "
                      f"file (expected one of {', '.join(names)})")


def optional_json_member(target: Path, name: str):
    """Same as read_json_member for a file that may legitimately be absent.

    Only looks inside a container. A bare .json file cannot also contain a
    second member, and the suffix fallback in read_json_member would otherwise
    hand back the file the caller already read under a different name.
    """
    if target.is_file() and not zipfile.is_zipfile(target):
        return None
    try:
        parsed, _ = read_json_member(target, name)
        return parsed
    except DomainError:
        return None


def _load_json(raw: bytes, name: str):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise DomainError(f"{name} is not valid UTF-8: {e}") from e
    except json.JSONDecodeError as e:
        raise DomainError(f"{name} is not valid JSON: line {e.lineno} "
                          f"column {e.colno}: {e.msg}") from e


# ---------------------------------------------------------------- registry

def registry() -> dict[str, Connector]:
    """Every module in this package that defines `name` and `scan()`.

    Discovered rather than listed, so adding a connector is adding one file;
    helper modules (base, typedstream, ...) define no `scan` and are skipped.
    """
    import importlib
    import pkgutil

    from .. import connectors as pkg

    found: dict[str, Connector] = {}
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if info.name.startswith("_") or info.name == "base":
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        if callable(getattr(mod, "scan", None)) and isinstance(
                getattr(mod, "name", None), str):
            found[mod.name] = mod
    return found


def get(name: str) -> Connector:
    reg = registry()
    if name not in reg:
        raise DomainError(f"unknown connector '{name}' "
                          f"(available: {', '.join(sorted(reg))})")
    return reg[name]

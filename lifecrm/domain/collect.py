"""The daily collection's outcomes (schema v8, `collect_runs`).

`lifecrm collect` (lifecrm/collect.py) decides, per source, whether a run
applied an import, found nothing new, found nothing at all, failed, or held
the import for the owner. A hold writes nothing to the records, so the
outcome is kept here, where `collect --status`, doctor and the nudges pack
read it. This is the only writer of the table: one row per source per run,
and one audit row with it.

Bookkeeping, not content: the as-of date the vault and the nudges pack are
dated by ignores these rows (domain/reminders.CONTENT_CHANGE), while the
after-write hook still re-renders the vault on them (REFRESH_CHANGE), so a
hold shows on Home the morning it happens.
"""

from __future__ import annotations

import json
import sqlite3

from . import audit
from .util import DomainError, canonical_json, is_iso_datetime

TABLE = "collect_runs"
OUTCOMES = ("applied", "unchanged", "held", "missing", "error")
HOLDS = ("first_import", "mass_removal", "invalid")
# What a source still needs from the owner: held, nothing found, failed.
ATTENTION = ("held", "missing", "error")


def record(conn: sqlite3.Connection, *, run_uuid: str, source: str, outcome: str,
           started_at: str, finished_at: str, hold: str | None = None,
           reason: str | None = None, target: str | None = None,
           import_uuid: str | None = None, counts: dict | None = None,
           actor: str = "system") -> dict:
    """Store one source's outcome of one run. The caller owns the
    transaction: an applied import and the row that says so commit together."""
    if not isinstance(run_uuid, str) or not run_uuid.strip():
        raise DomainError("a collect run needs a run uuid")
    if not isinstance(source, str) or not source.strip():
        raise DomainError("a collect run needs a source name")
    if outcome not in OUTCOMES:
        raise DomainError(f"collect outcome must be one of {', '.join(OUTCOMES)}, "
                          f"got {outcome!r}")
    if (outcome == "held") != (hold is not None):
        raise DomainError("a held source names the rule that held it, and only a "
                          "held one does")
    if hold is not None and hold not in HOLDS:
        raise DomainError(f"hold must be one of {', '.join(HOLDS)}, got {hold!r}")
    needs_reason = outcome in ATTENTION
    if needs_reason and (not isinstance(reason, str) or not reason.strip()):
        raise DomainError(f"a {outcome} source needs a one-line reason")
    if not needs_reason and reason is not None:
        raise DomainError(f"an {outcome} source carries no reason")
    for label, v in (("started_at", started_at), ("finished_at", finished_at)):
        if not isinstance(v, str) or not is_iso_datetime(v):
            raise DomainError(f"collect {label} must be an ISO instant, got {v!r}")
    reason = " ".join(reason.split()) if reason else None     # one line, always
    cur = conn.execute(
        f"INSERT INTO {TABLE} (run_uuid, source, outcome, hold, reason, target, "
        f"import_uuid, counts_json, started_at, finished_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_uuid, source, outcome, hold, reason, target, import_uuid,
         canonical_json(counts) if counts else None, started_at, finished_at))
    audit.log(conn, actor, outcome, TABLE, run_uuid,
              {"source": source, "hold": hold, "import_uuid": import_uuid})
    return _row(conn.execute(f"SELECT * FROM {TABLE} WHERE id=?",
                             (cur.lastrowid,)).fetchone())


def latest(conn: sqlite3.Connection) -> dict[str, dict]:
    """{source: its newest row}, for every source ever collected."""
    return {r["source"]: _row(r) for r in conn.execute(
        f"SELECT c.* FROM {TABLE} c WHERE c.id = "
        f"(SELECT MAX(d.id) FROM {TABLE} d WHERE d.source = c.source) "
        f"ORDER BY c.source")}


def last_run_at(conn: sqlite3.Connection) -> str | None:
    """When the newest run started, or None before the first."""
    row = conn.execute(f"SELECT MAX(started_at) FROM {TABLE}").fetchone()
    return row[0] if row else None


def last_applied(conn: sqlite3.Connection) -> dict[str, str]:
    """{source: when it last applied an import}."""
    return {r["source"]: r["at"] for r in conn.execute(
        f"SELECT source, MAX(started_at) AS at FROM {TABLE} "
        f"WHERE outcome = 'applied' GROUP BY source")}


def _row(r: sqlite3.Row) -> dict:
    out = dict(r)
    out["counts"] = json.loads(out.pop("counts_json")) if r["counts_json"] else {}
    return out

"""Append-only audit log — every domain mutation records one row."""

from __future__ import annotations

import json
import sqlite3

from .util import now_utc


def log(conn: sqlite3.Connection, actor: str, action: str, table_name: str,
        row_uuid: str | None = None, details: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, table_name, row_uuid, details) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now_utc(), actor, action, table_name, row_uuid,
         json.dumps(details, ensure_ascii=False) if details is not None else None),
    )

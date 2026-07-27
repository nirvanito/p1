"""Connection factory, schema initialization, and the migrations runner.

Read-only connections use the SQLite URI mode=ro plus PRAGMA query_only=1 —
statement-text parsing is never used as a write guard.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import SCHEMA_VERSION, CONTRACT_VERSION
from .config import DEFAULT_CATEGORIES, DEFAULT_KINDS

SCHEMA_FILE = Path(__file__).parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def connect(db_path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only=1")
    else:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path, now: str) -> None:
    """Create a fresh database from schema.sql and seed reference tables."""
    if Path(db_path).exists():
        raise FileExistsError(f"database already exists: {db_path}")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [("schema_version", str(SCHEMA_VERSION)),
             ("contract_version", str(CONTRACT_VERSION))],
        )
        conn.executemany(
            "INSERT INTO categories (name, created_at) VALUES (?, ?)",
            [(c, now) for c in DEFAULT_CATEGORIES],
        )
        conn.executemany(
            "INSERT INTO entry_kinds (name, created_at) VALUES (?, ?)",
            [(k, now) for k in DEFAULT_KINDS],
        )
        conn.commit()
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise RuntimeError("meta.schema_version missing — not a lifecrm database?")
    return int(row["value"])


def available_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    out = []
    if migrations_dir.exists():
        for f in sorted(migrations_dir.iterdir()):
            m = _MIGRATION_RE.match(f.name)
            if m:
                out.append((int(m.group(1)), f))
    return out


def pending_migrations(conn: sqlite3.Connection,
                       migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    current = schema_version(conn)
    return [(v, f) for v, f in available_migrations(migrations_dir) if v > current]


def migrate(conn: sqlite3.Connection, *, snapshot_to: Path | None = None,
            migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations in order, each in its own transaction.

    When snapshot_to is given, a backup-API snapshot is written there before
    anything is applied.
    """
    pending = pending_migrations(conn, migrations_dir)
    if not pending:
        return []
    if snapshot_to is not None:
        snapshot(conn, snapshot_to)
    applied = []
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)  # opens its own implicit transaction
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(version),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append(version)
    return applied


def snapshot(conn: sqlite3.Connection, dest: Path) -> Path:
    """Consistent snapshot via the SQLite backup API — never a raw file copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        with target:
            conn.backup(target)
    finally:
        target.close()
    return dest

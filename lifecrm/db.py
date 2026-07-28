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
_COMMENT_LINE_RE = re.compile(r"^\s*(--.*)?$")


class MigrationError(RuntimeError):
    """A malformed migration set: duplicate numbers, gaps, or reserved numbers."""


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
    """All migrations, validated: numbers start at 2 (schema.sql is v1), no
    duplicates, no gaps. A malformed set raises before anything applies."""
    found: dict[int, Path] = {}
    if migrations_dir.exists():
        for f in sorted(migrations_dir.iterdir()):
            m = _MIGRATION_RE.match(f.name)
            if not m:
                continue
            version = int(m.group(1))
            if version < 2:
                raise MigrationError(
                    f"migration number {version:04d} is reserved ({f.name}) — "
                    f"schema.sql is version 1; migrations start at 0002")
            if version in found:
                raise MigrationError(
                    f"duplicate migration number {version:04d}: "
                    f"{found[version].name} and {f.name}")
            found[version] = f
    versions = sorted(found)
    for i, version in enumerate(versions):
        expected = 2 + i
        if version != expected:
            raise MigrationError(
                f"gap in migration sequence: found {found[version].name} but no "
                f"migration {expected:04d} (have: "
                f"{', '.join(found[v].name for v in versions)})")
    return [(v, found[v]) for v in versions]


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into complete statements.

    `sqlite3.complete_statement` (sqlite3_complete) tokenizes for real: a
    semicolon inside a string literal, a comment, or a CREATE TRIGGER
    BEGIN...END body does not terminate a statement. We accumulate text and
    only cut where a candidate chunk is a complete statement — so trigger
    bodies like the ones in schema.sql survive intact. A naive text split on
    ';' would not.
    """
    statements: list[str] = []
    buf: list[str] = []
    for ch in sql:
        buf.append(ch)
        if ch == ";" and sqlite3.complete_statement("".join(buf)):
            stmt = "".join(buf).strip()
            if not all(_COMMENT_LINE_RE.match(line) for line in stmt.splitlines()):
                statements.append(stmt)
            buf = []
    tail = "".join(buf).strip()
    if tail and not all(_COMMENT_LINE_RE.match(line) for line in tail.splitlines()):
        statements.append(tail)  # unterminated tail: let SQLite report the error
    return statements


def pending_migrations(conn: sqlite3.Connection,
                       migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    current = schema_version(conn)
    return [(v, f) for v, f in available_migrations(migrations_dir) if v > current]


def migrate(conn: sqlite3.Connection, *, snapshot_to: Path | None = None,
            migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations in order, each atomically.

    NOT executescript: that commits any pending transaction and then runs each
    statement in autocommit mode, so a failure half-way leaves earlier
    statements applied with schema_version claiming otherwise. Instead each
    migration's statements AND its schema_version bump run inside one explicit
    BEGIN IMMEDIATE ... COMMIT — either the whole migration lands or none of it.
    (DDL is transactional in SQLite, so this is safe.)

    A migration that changes the contract must update meta.contract_version
    itself — the runner only maintains schema_version.

    When snapshot_to is given, a backup-API snapshot is written there before
    anything is applied. Any transaction the caller left open is committed
    first — do not call this mid-transaction.
    """
    pending = pending_migrations(conn, migrations_dir)
    if not pending:
        return []
    if snapshot_to is not None:
        snapshot(conn, snapshot_to)
    applied = []
    for version, path in pending:
        statements = split_statements(path.read_text(encoding="utf-8-sig"))
        conn.commit()  # clean autocommit state before the explicit transaction
        try:
            conn.execute("BEGIN IMMEDIATE")
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                         (str(version),))
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

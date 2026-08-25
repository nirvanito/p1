import sqlite3

import pytest

from lifecrm import SCHEMA_VERSION
from lifecrm import db as db_mod
from lifecrm.domain import corrections, entries
from lifecrm.importer import import_text


def test_readonly_connection_blocks_writes(conn, cfg):
    ro = db_mod.connect(cfg.db_path, readonly=True)
    try:
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO days (journal_date, created_at, updated_at) "
                       "VALUES ('2026-01-01', 'x', 'x')")
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM categories")
    finally:
        ro.close()


def test_fts_triggers_track_entries(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="Lunch: chicken burrito bowl", primary_category="food")
    hit = conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'burrito'"
                       ).fetchone()
    assert hit is not None

    corrections.edit_entry(conn, e["uuid"], {"title": "Lunch: carnitas tacos"})
    assert conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'burrito'"
                        ).fetchone() is None
    assert conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'carnitas'"
                        ).fetchone() is not None

    corrections.purge_day(conn, "2026-07-27")
    assert conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'carnitas'"
                        ).fetchone() is None


def test_fts_tracks_transcripts(conn, cfg):
    import_text(conn, cfg, "I finally sent the blue V4 at the gym.",
                journal_date="2026-07-27")
    hit = conn.execute(
        "SELECT rowid FROM transcripts_fts WHERE transcripts_fts MATCH 'gym'").fetchone()
    assert hit is not None


def test_fts_snippet(conn):
    entries.create(conn, journal_date="2026-07-27", kind="event",
                   title="Bouldering at Movement",
                   body="Finally sent the blue V4 after months of attempts.",
                   primary_category="exercise")
    snip = conn.execute(
        "SELECT snippet(entries_fts, 1, '[', ']', '...', 8) AS s "
        "FROM entries_fts WHERE entries_fts MATCH 'V4'").fetchone()["s"]
    assert "[V4]" in snip


def test_migration_framework(v1_conn, cfg, tmp_path):
    conn = v1_conn
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_add_widgets.sql").write_text(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT);\n", encoding="utf-8")
    (mig / "0003_add_gadgets.sql").write_text(
        "CREATE TABLE gadgets (id INTEGER PRIMARY KEY);\n", encoding="utf-8")

    assert db_mod.schema_version(conn) == 1
    snap = cfg.backups_dir / "pre-migration-test.db"
    applied = db_mod.migrate(conn, snapshot_to=snap, migrations_dir=mig)
    assert applied == [2, 3]
    assert db_mod.schema_version(conn) == 3
    conn.execute("INSERT INTO widgets (name) VALUES ('w')")   # table exists
    assert snap.exists()                                       # pre-migration snapshot

    # snapshot still opens and is at the old version (rollback path)
    old = sqlite3.connect(str(snap))
    try:
        v = old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert v == "1"
    finally:
        old.close()

    # re-running is a no-op
    assert db_mod.migrate(conn, migrations_dir=mig) == []


def test_migration_failure_rolls_back(v1_conn, tmp_path):
    conn = v1_conn
    """Parse failure: dies before anything applies."""
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_bad.sql").write_text("CREATE TABLE broken (;\n", encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError):
        db_mod.migrate(conn, migrations_dir=mig)
    assert db_mod.schema_version(conn) == 1


def test_migration_create_then_fail_is_atomic(v1_conn, tmp_path):
    conn = v1_conn
    """Runtime failure AFTER a successful statement: the earlier statement must
    not survive. This is the executescript autocommit bug F0 exists to fix."""
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_half.sql").write_text(
        "CREATE TABLE half_applied (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO nonexistent_table VALUES (1);\n", encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError):
        db_mod.migrate(conn, migrations_dir=mig)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "half_applied" not in tables
    assert db_mod.schema_version(conn) == 1


def test_migration_duplicate_numbers_rejected(v1_conn, tmp_path):
    conn = v1_conn
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_first.sql").write_text("CREATE TABLE a (id INTEGER);\n", encoding="utf-8")
    (mig / "0002_second.sql").write_text("CREATE TABLE b (id INTEGER);\n", encoding="utf-8")
    with pytest.raises(db_mod.MigrationError, match="0002_first.sql and 0002_second.sql"):
        db_mod.migrate(conn, migrations_dir=mig)
    assert db_mod.schema_version(conn) == 1


def test_migration_gap_rejected(v1_conn, tmp_path):
    conn = v1_conn
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_ok.sql").write_text("CREATE TABLE a (id INTEGER);\n", encoding="utf-8")
    (mig / "0004_skips.sql").write_text("CREATE TABLE b (id INTEGER);\n", encoding="utf-8")
    with pytest.raises(db_mod.MigrationError, match="0004_skips.sql but no migration 0003"):
        db_mod.migrate(conn, migrations_dir=mig)
    assert db_mod.schema_version(conn) == 1


def test_migration_reserved_number_rejected(v1_conn, tmp_path):
    conn = v1_conn
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0001_schema.sql").write_text("CREATE TABLE a (id INTEGER);\n", encoding="utf-8")
    with pytest.raises(db_mod.MigrationError, match="reserved"):
        db_mod.migrate(conn, migrations_dir=mig)


def test_migration_can_update_contract_version(v1_conn, tmp_path):
    conn = v1_conn
    """Contract bumps ride inside the migration's transaction."""
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_contract.sql").write_text(
        "CREATE TABLE new_thing (id INTEGER PRIMARY KEY);\n"
        "UPDATE meta SET value='99' WHERE key='contract_version';\n", encoding="utf-8")
    db_mod.migrate(conn, migrations_dir=mig)
    assert db_mod.schema_version(conn) == 2
    row = conn.execute("SELECT value FROM meta WHERE key='contract_version'").fetchone()
    assert row["value"] == "99"


def test_split_statements_survives_triggers_and_literals(tmp_path):
    """Splitting schema.sql (trigger bodies with internal semicolons) and
    executing statement-by-statement must reproduce exactly what executescript
    produces on a fresh database."""
    schema_sql = db_mod.SCHEMA_FILE.read_text(encoding="utf-8")
    statements = db_mod.split_statements(schema_sql)
    assert len(statements) > 20

    ref = sqlite3.connect(":memory:")
    ref.executescript(schema_sql)
    split = sqlite3.connect(":memory:")
    split.execute("BEGIN")
    for stmt in statements:
        split.execute(stmt)
    split.commit()
    q = ("SELECT type, name, COALESCE(sql,'') FROM sqlite_master "
         "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")
    assert ref.execute(q).fetchall() == split.execute(q).fetchall()
    ref.close()
    split.close()

    # semicolon inside a string literal does not split
    stmts = db_mod.split_statements(
        "INSERT INTO t VALUES ('a;b');\nINSERT INTO t VALUES ('c');\n")
    assert len(stmts) == 2
    assert stmts[0] == "INSERT INTO t VALUES ('a;b');"


def test_out_of_date_schema_detected(v1_conn, tmp_path):
    conn = v1_conn
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_x.sql").write_text("CREATE TABLE x (id INTEGER);\n", encoding="utf-8")
    pending = db_mod.pending_migrations(conn, mig)
    assert [v for v, _ in pending] == [2]


def _normalized_master(c) -> dict:
    """sqlite_master with comments stripped, quotes removed, whitespace
    collapsed — the form in which a migrated schema and a fresh init must be
    IDENTICAL. Drift between those paths surfaces years later, during a
    restore, on a bad day."""
    import re
    out = {}
    for r in c.execute("SELECT type, name, sql FROM sqlite_master "
                       "WHERE name NOT LIKE 'sqlite_%'"):
        sql = r["sql"] or ""
        sql = re.sub(r"--[^\n]*", "", sql)
        sql = sql.replace('"', "")
        sql = " ".join(sql.split())
        sql = re.sub(r"\s*([(),])\s*", r"\1", sql)   # token-boundary spacing
        out[(r["type"], r["name"])] = sql
    return out


def test_real_migration_chain_v1_to_latest(v1_conn):
    """The shipped migration chain on a pinned v1 database, with data."""
    conn = v1_conn
    # seed v1 rows that the migration must carry over
    conn.execute("INSERT INTO days (journal_date, created_at, updated_at) "
                 "VALUES ('2026-07-27', 'x', 'x')")
    conn.execute("INSERT INTO sources (uuid, type, timezone, journal_date, created_at) "
                 "VALUES ('s-uuid', 'pasted', 'America/Chicago', '2026-07-27', 'x')")
    conn.execute("INSERT INTO extraction_runs (uuid, source_id, provider, interface, "
                 "contract_version, input_hash, started_at, status) "
                 "VALUES ('r-uuid', 1, 'test', 'manual', 1, 'h', 'x', 'applied')")
    conn.commit()

    applied = db_mod.migrate(conn)
    assert applied == list(range(2, SCHEMA_VERSION + 1))
    assert db_mod.schema_version(conn) == SCHEMA_VERSION
    assert conn.execute("SELECT value FROM meta WHERE key='contract_version'"
                        ).fetchone()["value"] == "2"   # 0003 is schema-only
    # the singular pointer moved into the join table
    row = conn.execute("SELECT * FROM extraction_run_sources").fetchone()
    assert (row["extraction_run_id"], row["source_id"]) == (1, 1)
    # the run row survived the table rebuild
    run = conn.execute("SELECT * FROM extraction_runs WHERE uuid='r-uuid'").fetchone()
    assert run["provider"] == "test" and run["status"] == "applied"
    # new columns and tables exist
    conn.execute("SELECT closed_at, closure FROM entries LIMIT 0")
    conn.execute("SELECT * FROM entry_links LIMIT 0")
    conn.execute("SELECT * FROM health_records LIMIT 0")
    for t in ("imports", "observations", "entity_handles",
              "observation_participants", "entry_observations"):
        conn.execute(f"SELECT * FROM {t} LIMIT 0")


def test_migrated_schema_matches_fresh_init(v1_conn, tmp_path):
    """Parity: pinned v1 + migration chain == fresh v2 init, table by table."""
    from lifecrm.domain.util import now_utc
    conn = v1_conn
    db_mod.migrate(conn)
    fresh_path = tmp_path / "fresh.db"
    db_mod.init_db(fresh_path, now_utc())
    fresh = db_mod.connect(fresh_path)
    try:
        migrated = _normalized_master(conn)
        pristine = _normalized_master(fresh)
        assert migrated == pristine
    finally:
        fresh.close()


def test_snapshot_is_consistent_backup(conn, cfg):
    entries.create(conn, journal_date="2026-07-27", kind="event",
                   title="X", primary_category="food")
    conn.commit()
    dest = cfg.backups_dir / "snap.db"
    db_mod.snapshot(conn, dest)
    other = sqlite3.connect(str(dest))
    try:
        assert other.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
        assert other.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        other.close()

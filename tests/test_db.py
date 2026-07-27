import sqlite3

import pytest

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


def test_migration_framework(conn, cfg, tmp_path):
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


def test_migration_failure_rolls_back(conn, tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_bad.sql").write_text("CREATE TABLE broken (;\n", encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError):
        db_mod.migrate(conn, migrations_dir=mig)
    assert db_mod.schema_version(conn) == 1


def test_out_of_date_schema_detected(conn, tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_x.sql").write_text("CREATE TABLE x (id INTEGER);\n", encoding="utf-8")
    pending = db_mod.pending_migrations(conn, mig)
    assert [v for v, _ in pending] == [2]


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

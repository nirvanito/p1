"""Each check must actually fire. Direct SQL is correct here — the tests
simulate exactly the corruption the domain layer must never produce."""

from conftest import golden_draft

from lifecrm.domain import corrections, entities, entries, links, reconcile
from lifecrm.doctor import run_checks
from test_reconcile import _ingest


def _seed(conn, cfg):
    diff = _ingest(conn, cfg, golden_draft())
    reconcile.apply_diff(conn, diff)
    conn.commit()


def _status(report, name):
    return next(c for c in report["checks"] if c["check"] == name)["status"]


def test_healthy_database_passes(conn, cfg):
    _seed(conn, cfg)
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"]
                                     if c["status"] == "fail"]
    assert report["healthy"] is True


def test_zero_primary_category_detected(conn, cfg):
    _seed(conn, cfg)
    conn.execute("DELETE FROM entry_categories WHERE is_primary=1 AND entry_id="
                 "(SELECT id FROM entries LIMIT 1)")
    report = run_checks(conn, cfg)
    assert _status(report, "one_primary_category") == "fail"


def test_two_primaries_detected(conn, cfg):
    _seed(conn, cfg)
    conn.execute("DROP INDEX idx_entry_primary_category")   # remove the backstop
    conn.execute("INSERT INTO entry_categories (entry_id, category_id, is_primary) "
                 "SELECT (SELECT id FROM entries LIMIT 1), id, 1 FROM categories "
                 "WHERE name='other'")
    report = run_checks(conn, cfg)
    assert _status(report, "one_primary_category") == "fail"


def test_fts_desync_detected(conn, cfg):
    _seed(conn, cfg)
    # tombstone one row out of the index while the content table keeps it
    row = conn.execute("SELECT id, title, body FROM entries LIMIT 1").fetchone()
    conn.execute("INSERT INTO entries_fts(entries_fts, rowid, title, body) "
                 "VALUES ('delete', ?, ?, ?)",
                 (row["id"], row["title"], row["body"]))
    report = run_checks(conn, cfg)
    assert _status(report, "fts_sync_entries_fts") == "fail"


def test_entry_without_provenance_detected(conn, cfg):
    _seed(conn, cfg)
    entries.create(conn, journal_date="2026-07-27", kind="event",
                   title="No provenance", primary_category="other")
    report = run_checks(conn, cfg)
    assert _status(report, "entries_have_provenance") == "fail"


def test_source_without_transcript_detected(conn, cfg):
    from lifecrm.domain import sources
    _seed(conn, cfg)
    sources.create(conn, source_type="manual", timezone_name="America/Chicago",
                   journal_date="2026-07-27")
    report = run_checks(conn, cfg)
    assert _status(report, "sources_have_transcripts") == "fail"


def test_mention_on_merged_entity_detected(conn, cfg):
    _seed(conn, cfg)
    jake = conn.execute("SELECT id FROM entities WHERE name='Jake'").fetchone()["id"]
    reilly = entities.create(conn, "person", "Jake Reilly")
    entities.merge(conn, jake, reilly["id"])   # moves mentions correctly
    # now simulate the corruption: a mention pointing back at the merged row
    conn.execute("INSERT INTO mentions (entry_id, entity_id, role) "
                 "SELECT id, ?, 'mention' FROM entries LIMIT 1", (jake,))
    report = run_checks(conn, cfg)
    assert _status(report, "no_mentions_on_merged_entities") == "fail"


def test_measurement_owner_xor_detected(conn, cfg):
    _seed(conn, cfg)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute("UPDATE measurements SET journal_date=NULL, entry_id=NULL "
                 "WHERE uuid=(SELECT uuid FROM measurements LIMIT 1)")
    conn.execute("PRAGMA ignore_check_constraints=OFF")
    report = run_checks(conn, cfg)
    assert _status(report, "measurement_owner_xor") == "fail"


def test_closure_pairing_detected(conn, cfg):
    _seed(conn, cfg)
    conn.execute("UPDATE entries SET closure='done' "
                 "WHERE id=(SELECT id FROM entries LIMIT 1)")
    report = run_checks(conn, cfg)
    assert _status(report, "closure_pairing") == "fail"


def test_superseded_consistency_detected(conn, cfg):
    _seed(conn, cfg)
    conn.execute("UPDATE entries SET status='superseded' "
                 "WHERE id=(SELECT id FROM entries LIMIT 1)")
    report = run_checks(conn, cfg)
    assert _status(report, "superseded_consistency") == "fail"


def test_run_sources_inconsistency_detected(conn, cfg):
    _seed(conn, cfg)
    conn.execute("DELETE FROM extraction_run_sources")
    report = run_checks(conn, cfg)
    assert _status(report, "run_sources_consistent") == "fail"


def test_schema_version_mismatch_detected(conn, cfg):
    _seed(conn, cfg)
    conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    report = run_checks(conn, cfg)
    assert _status(report, "schema_version") == "fail"
    assert report["healthy"] is False


def test_overdue_loop_warns(conn, cfg):
    _seed(conn, cfg)
    entries.create(conn, journal_date="2026-07-27", kind="task",
                   title="Ancient task", primary_category="chores",
                   due_at="2020-01-01")
    # give it provenance so only the warning fires
    eid = conn.execute("SELECT id FROM entries WHERE title='Ancient task'"
                       ).fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()["id"]
    entries.add_provenance(conn, entry_id=eid, source_id=sid)
    report = run_checks(conn, cfg)
    assert report["failures"] == 0
    assert _status(report, "overdue_loops") == "warn"


def test_backup_age_warns_without_snapshots(conn, cfg):
    _seed(conn, cfg)
    report = run_checks(conn, cfg)
    assert _status(report, "backup_age") == "warn"   # no snapshots yet


def test_links_resolve_check_passes_after_purge(conn, cfg):
    """CASCADE means purge leaves no dangling links for doctor to find."""
    _seed(conn, cfg)
    a = conn.execute("SELECT uuid FROM entries LIMIT 1").fetchone()["uuid"]
    b = conn.execute("SELECT uuid FROM entries WHERE uuid<>? LIMIT 1",
                     (a,)).fetchone()["uuid"]
    links.add(conn, a, b, "relates")
    corrections.purge_day(conn, "2026-07-27")
    report = run_checks(conn, cfg)
    assert _status(report, "links_resolve") == "ok"
    assert conn.execute("SELECT COUNT(*) c FROM entry_links").fetchone()["c"] == 0

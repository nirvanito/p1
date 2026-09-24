"""Each check must actually fire. Direct SQL is correct here — the tests
simulate exactly the corruption the domain layer must never produce."""

import pytest
from conftest import FIXTURES, golden_draft

from lifecrm import health_connect as hc
from lifecrm.connectors import ai_chat
from lifecrm.domain import (corrections, entities, entries, health, links,
                            measurements, observations as obs, reconcile)
from lifecrm.doctor import run_checks
from test_health_connect import make_hc_db
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


# ---------------------------------------------------------------- every op

# The invariants are only worth having if every write path preserves them.
# `entry merge` shipped destroying entry_sources rows and left doctor failing
# forever after; nothing noticed because no test ran doctor after a merge.
# So: seed a real day, run one domain operation, and demand a green report —
# once per operation, for every operation that writes.

def _uuids(conn):
    return [r["uuid"] for r in conn.execute("SELECT uuid FROM entries ORDER BY id")]


def _ids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM entries ORDER BY id")]


def _person_ids(conn):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM entities WHERE type='person' AND status='active' ORDER BY id")]


def _op_edit(conn, cfg, tmp_path):
    corrections.edit_entry(conn, _uuids(conn)[0],
                           {"title": "Edited title", "secondary_categories": ["social"]})


def _op_retract(conn, cfg, tmp_path):
    corrections.retract_entry(conn, _uuids(conn)[0], reason="did not happen")


def _op_restore(conn, cfg, tmp_path):
    u = _uuids(conn)[0]
    corrections.retract_entry(conn, u, reason="did not happen")
    corrections.restore_entry(conn, u)


def _op_close(conn, cfg, tmp_path):
    corrections.close_entry(conn, _uuids(conn)[0], "done")


def _op_reopen(conn, cfg, tmp_path):
    u = _uuids(conn)[0]
    corrections.close_entry(conn, u, "done")
    corrections.reopen_entry(conn, u)


def _op_entry_merge(conn, cfg, tmp_path):
    a, b = _uuids(conn)[:2]
    corrections.merge_entries(conn, a, b)


def _op_entity_merge(conn, cfg, tmp_path):
    people = _person_ids(conn)
    assert len(people) >= 2, "fixture should create at least two people"
    entities.merge(conn, people[0], people[1])


def _op_entity_alias(conn, cfg, tmp_path):
    entities.add_alias(conn, _person_ids(conn)[0], "Danna")


def _op_entity_rename(conn, cfg, tmp_path):
    entities.rename(conn, _person_ids(conn)[0], "Dana R.")


def _op_link_add(conn, cfg, tmp_path):
    a, b = _uuids(conn)[:2]
    links.add(conn, a, b, "relates", note="same morning")


def _op_link_remove(conn, cfg, tmp_path):
    a, b = _uuids(conn)[:2]
    links.add(conn, a, b, "relates")
    links.remove(conn, a, b, "relates")


def _op_measurement_add(conn, cfg, tmp_path):
    measurements.add(conn, entry_id=_ids(conn)[0], metric="duration",
                     numeric_value=45, unit="min")


def _op_measurement_retract(conn, cfg, tmp_path):
    m = measurements.add(conn, entry_id=_ids(conn)[0], metric="duration",
                         numeric_value=45, unit="min")
    measurements.retract(conn, m["uuid"])


def _op_purge_day(conn, cfg, tmp_path):
    corrections.purge_day(conn, "2026-07-27")


def _op_health_import(conn, cfg, tmp_path):
    z = tmp_path / "hc_export.zip"
    db = tmp_path / "healthconnect.db"
    make_hc_db(db)
    import zipfile
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(db, "healthconnect.db")
    scan = hc.read_export(z, cfg)
    hc.apply_plan(conn, hc.build_plan(conn, scan["records"]))


def _op_health_retract(conn, cfg, tmp_path):
    _op_health_import(conn, cfg, tmp_path)
    health.retract_source_file(conn, "hc_export.zip")


def _op_health_restore(conn, cfg, tmp_path):
    _op_health_retract(conn, cfg, tmp_path)
    health.restore_source_file(conn, "hc_export.zip")


def _ai_chat_scan(cfg, tmp_path):
    export = tmp_path / "conversations.json"
    export.write_bytes((FIXTURES / "ai_chat_claude.json").read_bytes())
    return ai_chat.scan(export, cfg)


def _op_observe_apply(conn, cfg, tmp_path):
    scan = _ai_chat_scan(cfg, tmp_path)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))


def _op_observe_retract(conn, cfg, tmp_path):
    scan = _ai_chat_scan(cfg, tmp_path)
    result = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    obs.retract_import(conn, result["import_uuid"])


def _op_observe_ios_messages(conn, cfg, tmp_path):
    from test_ios_messages import make_backup
    from lifecrm.connectors import ios_messages
    scan = ios_messages.scan(make_backup(tmp_path / "backup"), cfg)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))


def _op_handle_link(conn, cfg, tmp_path):
    scan = _ai_chat_scan(cfg, tmp_path)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    handle = conn.execute("SELECT kind, value FROM entity_handles LIMIT 1").fetchone()
    obs.link_handle(conn, handle["kind"], handle["value"], _person_ids(conn)[0])


DOMAIN_OPS = {
    "entry edit": _op_edit,
    "entry retract": _op_retract,
    "entry restore": _op_restore,
    "entry close": _op_close,
    "entry reopen": _op_reopen,
    "entry merge": _op_entry_merge,
    "entity merge": _op_entity_merge,
    "entity alias-add": _op_entity_alias,
    "entity rename": _op_entity_rename,
    "link add": _op_link_add,
    "link rm": _op_link_remove,
    "measurement add": _op_measurement_add,
    "measurement retract": _op_measurement_retract,
    "admin purge-day": _op_purge_day,
    "health import": _op_health_import,
    "health retract": _op_health_retract,
    "health restore": _op_health_restore,
    "observe apply": _op_observe_apply,
    "observe retract": _op_observe_retract,
    "observe ios_messages": _op_observe_ios_messages,
    "handles link": _op_handle_link,
}


@pytest.mark.parametrize("op", list(DOMAIN_OPS))
def test_doctor_stays_green_after(op, conn, cfg, tmp_path):
    _seed(conn, cfg)
    assert run_checks(conn, cfg)["failures"] == 0, "seed itself is not green"

    DOMAIN_OPS[op](conn, cfg, tmp_path)
    conn.commit()

    report = run_checks(conn, cfg)
    assert report["failures"] == 0, (
        f"{op} broke: " + str([(c["check"], c.get("detail"))
                               for c in report["checks"] if c["status"] == "fail"]))

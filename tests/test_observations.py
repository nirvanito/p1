"""The observed tier's shared write layer.

Connectors are pure readers; everything asserted here is written once and used
by every connector that follows. The load-bearing claims are: re-importing is
safe, only strictly newer records win, conflicts are surfaced rather than
applied, retraction is precise, and confirming ONE handle attributes every
observation that ever referenced it — with no backfill pass.
"""

import json

import pytest
from conftest import FIXTURES, golden_draft

from lifecrm.config import Config
from lifecrm.connectors import ai_chat
from lifecrm.domain import entities, observations as obs, reconcile
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks
from test_reconcile import _ingest


def _export(tmp_path):
    p = tmp_path / "conversations.json"
    p.write_bytes((FIXTURES / "ai_chat_claude.json").read_bytes())
    return p


def _scan(tmp_path, cfg):
    return ai_chat.scan(_export(tmp_path), cfg)


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _status(report, name):
    return next(c for c in report["checks"] if c["check"] == name)["status"]


# ---------------------------------------------------------------- validation

def _rec(**kw):
    base = {"stream": "s", "external_id": "x", "journal_date": "2026-03-10",
            "started_at": "2026-03-10T15:00:00Z", "body": "hi"}
    base.update(kw)
    return base


def test_validate_accepts_a_minimal_record():
    assert obs.validate(_rec()) is None


@pytest.mark.parametrize("bad, fragment", [
    ({"stream": ""}, "non-empty 'stream'"),
    ({"external_id": None}, "non-empty 'external_id'"),
    ({"journal_date": ""}, "non-empty 'journal_date'"),
    ({"direction": "sideways"}, "direction 'sideways'"),
    ({"ended_at": "2026-03-09T00:00:00Z"}, "ends before it starts"),
    ({"value": "seven"}, "value must be a number"),
    ({"body": None, "title": None}, "has no content"),
])
def test_validate_rejects_with_one_line(bad, fragment):
    err = obs.validate(_rec(**bad))
    assert err is not None and fragment in err
    assert "\n" not in err


def test_validate_rejects_unknown_handle_kind_and_role():
    assert "handle kind" in obs.validate(
        _rec(participants=[{"kind": "telepathy", "value": "x", "role": "sender"}]))
    assert "role" in obs.validate(
        _rec(participants=[{"kind": "email", "value": "x", "role": "witness"}]))


# ---------------------------------------------------------------- classify

def test_apply_creates_rows_days_and_an_import(conn, cfg, tmp_path):
    scan = _scan(tmp_path, cfg)
    plan, result = _apply(conn, scan)
    assert result["added"] == 3
    rows = conn.execute("SELECT * FROM observations ORDER BY external_id").fetchall()
    assert [r["external_id"] for r in rows] == ["m1", "m2", "m5"]
    assert all(r["status"] == "active" for r in rows)
    # every observation's day row exists — nothing dangles
    assert conn.execute("SELECT COUNT(*) c FROM days WHERE journal_date="
                        "'2026-03-10'").fetchone()["c"] == 1
    imp = conn.execute("SELECT * FROM imports").fetchone()
    assert imp["status"] == "applied" and imp["connector"] == "ai_chat"
    assert json.loads(imp["counts_json"])["added"] == 3


def test_reimport_is_identical_not_duplicated(conn, cfg, tmp_path):
    scan = _scan(tmp_path, cfg)
    _apply(conn, scan)
    plan2, result2 = _apply(conn, scan)
    assert (result2["added"], result2["updated"]) == (0, 0)
    assert plan2["identical"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 3


def test_newer_record_updates_and_is_audited(conn, cfg, tmp_path):
    scan = _scan(tmp_path, cfg)
    _apply(conn, scan)
    edited = ai_chat.scan(_export(tmp_path), cfg)
    for rec in edited["records"]:
        if rec["external_id"] == "m1":
            rec["body"] = "Where is that lake town in Slovenia? (edited)"
            rec["last_modified_at"] = "2026-03-12T00:00:00Z"
    plan, result = _apply(conn, edited)
    assert result["updated"] == 1
    row = conn.execute("SELECT body FROM observations WHERE external_id='m1'"
                       ).fetchone()
    assert row["body"].endswith("(edited)")
    audit = conn.execute("SELECT * FROM audit_log WHERE table_name='observations' "
                         "AND action='update'").fetchone()
    assert "body" in json.loads(audit["details"])["changes"]


def test_changed_but_not_newer_is_a_conflict_and_is_never_applied(conn, cfg, tmp_path):
    scan = _scan(tmp_path, cfg)
    _apply(conn, scan)
    stale = ai_chat.scan(_export(tmp_path), cfg)
    for rec in stale["records"]:
        if rec["external_id"] == "m1":
            rec["body"] = "silently different"
            rec["last_modified_at"] = "2020-01-01T00:00:00Z"
    plan, result = _apply(conn, stale)
    assert result["conflicts"] == 1 and result["updated"] == 0
    assert plan["conflicts"][0]["changes"]["body"]["incoming"] == "silently different"
    row = conn.execute("SELECT body FROM observations WHERE external_id='m1'"
                       ).fetchone()
    assert row["body"] != "silently different"


def test_duplicate_ids_inside_one_export_are_counted_once(conn, cfg, tmp_path):
    scan = _scan(tmp_path, cfg)
    scan["records"] = scan["records"] + [dict(scan["records"][0])]
    plan, result = _apply(conn, scan)
    assert plan["dup_incoming"] == 1
    assert result["added"] == 3


def test_invalid_record_blocks_apply_entirely(conn, cfg, tmp_path):
    scan = _scan(tmp_path, cfg)
    scan["records"][0]["journal_date"] = ""
    plan = obs.build_plan(conn, scan)
    assert plan["invalid"]
    with pytest.raises(DomainError):
        obs.apply_plan(conn, scan, plan)


# ---------------------------------------------------------------- handles

def test_handles_are_recorded_but_never_auto_resolved(conn, cfg, tmp_path):
    _apply(conn, _scan(tmp_path, cfg))
    h = conn.execute("SELECT * FROM entity_handles").fetchone()
    assert h["kind"] == "app_account" and h["value"] == "claude:acct-1"
    assert h["entity_id"] is None, "an importer must never resolve an identity"
    assert h["observation_count"] == 3
    assert h["first_seen_at"] == "2026-03-10T15:04:05Z"


def test_linking_one_handle_attributes_every_observation(conn, cfg, tmp_path):
    """The whole reason the handle table exists: resolution is a join, so one
    decision covers everything that ever referenced the handle — past and
    future — with no backfill pass."""
    _apply(conn, _scan(tmp_path, cfg))
    before = conn.execute(_ATTRIBUTED).fetchone()["c"]
    assert before == 0

    ent = entities.create(conn, "person", "Casey")
    obs.link_handle(conn, "app_account", "claude:acct-1", ent["id"])
    conn.commit()

    assert conn.execute(_ATTRIBUTED).fetchone()["c"] == 3
    h = conn.execute("SELECT * FROM entity_handles").fetchone()
    assert h["entity_id"] == ent["id"] and h["confirmed_at"] is not None


_ATTRIBUTED = """
    SELECT COUNT(*) c FROM observations o
    JOIN observation_participants p ON p.observation_id = o.id
    JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value
    WHERE h.entity_id IS NOT NULL AND o.status = 'active'
"""


def test_a_later_import_inherits_the_resolution(conn, cfg, tmp_path):
    _apply(conn, _scan(tmp_path, cfg))
    ent = entities.create(conn, "person", "Casey")
    obs.link_handle(conn, "app_account", "claude:acct-1", ent["id"])
    conn.commit()

    later = _scan(tmp_path, cfg)
    later["records"].append({
        "stream": "claude_chat", "external_id": "m9",
        "journal_date": "2026-03-12", "started_at": "2026-03-12T12:00:00Z",
        "direction": "out", "title": "New thread", "body": "a later message",
        "origin_app": "claude", "last_modified_at": "2026-03-12T12:00:00Z",
        "participants": [{"kind": "app_account", "value": "claude:acct-1",
                          "role": "sender"}]})
    _apply(conn, later)
    assert conn.execute(_ATTRIBUTED).fetchone()["c"] == 4


def test_link_rejects_a_merged_or_missing_entity(conn, cfg, tmp_path):
    _apply(conn, _scan(tmp_path, cfg))
    a = entities.create(conn, "person", "Casey")
    b = entities.create(conn, "person", "Casey B")
    entities.merge(conn, a["id"], b["id"])
    with pytest.raises(DomainError) as e:
        obs.link_handle(conn, "app_account", "claude:acct-1", a["id"])
    assert "merged" in str(e.value)
    with pytest.raises(DomainError):
        obs.link_handle(conn, "app_account", "claude:acct-1", 9999)


def test_ignore_is_reversible(conn, cfg, tmp_path):
    _apply(conn, _scan(tmp_path, cfg))
    obs.ignore_handle(conn, "app_account", "claude:acct-1")
    assert obs.unresolved_handles(conn) == []
    ent = entities.create(conn, "person", "Casey")
    obs.link_handle(conn, "app_account", "claude:acct-1", ent["id"])
    h = conn.execute("SELECT * FROM entity_handles").fetchone()
    assert h["ignored"] == 0 and h["entity_id"] == ent["id"]


def test_link_unknown_handle_is_a_clear_error(conn):
    with pytest.raises(DomainError) as e:
        obs.link_handle(conn, "email", "nobody@example.com", 1)
    assert "nothing has referenced it" in str(e.value)


# ---------------------------------------------------------------- retraction

def test_retract_hides_rows_and_keeps_the_row_itself(conn, cfg, tmp_path):
    scan = _scan(tmp_path, cfg)
    _, result = _apply(conn, scan)
    out = obs.retract_import(conn, result["import_uuid"])
    conn.commit()
    assert out["retracted"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 3
    assert conn.execute("SELECT observation_count c FROM entity_handles"
                        ).fetchone()["c"] == 0


def test_retract_only_touches_rows_its_own_import_created(conn, cfg, tmp_path):
    """import_id names the run that CREATED a row. Retracting a later snapshot
    must not delete what an earlier one contributed."""
    first = _scan(tmp_path, cfg)
    _, r1 = _apply(conn, first)

    second = _scan(tmp_path, cfg)
    second["records"].append({
        "stream": "claude_chat", "external_id": "m9",
        "journal_date": "2026-03-12", "started_at": "2026-03-12T12:00:00Z",
        "direction": "out", "title": "New thread", "body": "only in export two",
        "origin_app": "claude", "last_modified_at": "2026-03-12T12:00:00Z",
        "participants": []})
    _, r2 = _apply(conn, second)
    assert r2["added"] == 1

    obs.retract_import(conn, r2["import_uuid"])
    conn.commit()
    live = [r["external_id"] for r in conn.execute(
        "SELECT external_id FROM observations WHERE status='active' "
        "ORDER BY external_id")]
    assert live == ["m1", "m2", "m5"], "the first import must survive"


def test_retract_twice_is_refused(conn, cfg, tmp_path):
    _, result = _apply(conn, _scan(tmp_path, cfg))
    obs.retract_import(conn, result["import_uuid"])
    with pytest.raises(DomainError) as e:
        obs.retract_import(conn, result["import_uuid"])
    assert "already retracted" in str(e.value)


def test_retract_unknown_import(conn):
    with pytest.raises(DomainError) as e:
        obs.retract_import(conn, "not-a-uuid")
    assert "no import with uuid" in str(e.value)


# ---------------------------------------------------------------- the tier rule

def test_importing_touches_no_confirmed_table(conn, cfg, tmp_path):
    """The rule the whole design rests on: an importer may not write entries,
    measurements, mentions or entity_aliases."""
    _apply(conn, _scan(tmp_path, cfg))
    for table in ("entries", "measurements", "mentions", "entity_aliases",
                  "entry_sources", "extraction_runs"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0, \
            f"the observed tier wrote to {table}"


def test_fts_finds_an_observation_by_conversation_name(conn, cfg, tmp_path):
    _apply(conn, _scan(tmp_path, cfg))
    hits = conn.execute(
        "SELECT o.external_id FROM observations_fts f "
        "JOIN observations o ON o.id = f.rowid "
        "WHERE observations_fts MATCH 'Slovenia' ORDER BY o.external_id").fetchall()
    assert [h["external_id"] for h in hits] == ["m1", "m2"]


# ---------------------------------------------------------------- doctor

def test_doctor_stays_green_through_the_whole_lifecycle(conn, cfg, tmp_path):
    scan = _scan(tmp_path, cfg)
    _, result = _apply(conn, scan)
    assert run_checks(conn, cfg)["failures"] == 0

    ent = entities.create(conn, "person", "Casey")
    obs.link_handle(conn, "app_account", "claude:acct-1", ent["id"])
    conn.commit()
    assert run_checks(conn, cfg)["failures"] == 0

    obs.retract_import(conn, result["import_uuid"])
    conn.commit()
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"]
                                     if c["status"] == "fail"]


def test_doctor_warns_about_unresolved_handles(conn, cfg, tmp_path):
    _apply(conn, _scan(tmp_path, cfg))
    report = run_checks(conn, cfg)
    assert _status(report, "unresolved_handles") == "warn"
    assert report["failures"] == 0


def test_doctor_catches_a_handle_pointing_at_a_merged_entity(conn, cfg, tmp_path):
    _apply(conn, _scan(tmp_path, cfg))
    a = entities.create(conn, "person", "Casey")
    b = entities.create(conn, "person", "Casey B")
    obs.link_handle(conn, "app_account", "claude:acct-1", a["id"])
    entities.merge(conn, a["id"], b["id"])
    conn.commit()
    report = run_checks(conn, cfg)
    assert _status(report, "handles_resolve") == "fail"


def test_doctor_catches_a_live_row_under_a_retracted_import(conn, cfg, tmp_path):
    _, result = _apply(conn, _scan(tmp_path, cfg))
    obs.retract_import(conn, result["import_uuid"])
    # exactly the corruption the domain layer must never produce
    conn.execute("UPDATE observations SET status='active' WHERE external_id='m1'")
    conn.commit()
    assert _status(run_checks(conn, cfg), "observations_import_live") == "fail"


def test_entry_provenance_may_come_from_either_tier(conn, cfg, tmp_path):
    """A confirmed entry sourced from an observation is still provenanced."""
    diff = _ingest(conn, cfg, golden_draft())
    reconcile.apply_diff(conn, diff)
    _apply(conn, _scan(tmp_path, cfg))
    conn.commit()
    assert run_checks(conn, cfg)["failures"] == 0

    entry_id = conn.execute("SELECT id FROM entries LIMIT 1").fetchone()["id"]
    obs_id = conn.execute("SELECT id FROM observations LIMIT 1").fetchone()["id"]
    conn.execute("DELETE FROM entry_sources WHERE entry_id=?", (entry_id,))
    conn.commit()
    assert _status(run_checks(conn, cfg), "entries_have_provenance") == "fail"

    conn.execute("INSERT INTO entry_observations (entry_id, observation_id) "
                 "VALUES (?, ?)", (entry_id, obs_id))
    conn.commit()
    assert _status(run_checks(conn, cfg), "entries_have_provenance") == "ok"


# ---------------------------------------------------------------- export

def test_export_covers_every_observed_table(conn, cfg, tmp_path):
    from lifecrm.export import export_jsonl
    _apply(conn, _scan(tmp_path, cfg))
    ent = entities.create(conn, "person", "Casey")
    obs.link_handle(conn, "app_account", "claude:acct-1", ent["id"])
    conn.commit()

    out = tmp_path / "archive"
    manifest = export_jsonl(conn, out)
    for table in ("imports", "observations", "entity_handles",
                  "observation_participants", "entry_observations"):
        assert table in manifest["tables"], f"{table} is missing from the export"
    assert manifest["tables"]["observations"] == 3
    rows = [json.loads(line) for line in
            (out / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["external_id"] for r in rows} == {"m1", "m2", "m5"}
    assert all(r["import_uuid"] for r in rows)
    handles = [json.loads(line) for line in
               (out / "entity_handles.jsonl").read_text(encoding="utf-8").splitlines()]
    assert handles[0]["entity_uuid"] == ent["uuid"], "ids must export as uuids"


def test_export_is_deterministic_for_observations(conn, cfg, tmp_path):
    from lifecrm.export import export_jsonl
    _apply(conn, _scan(tmp_path, cfg))
    a, b = tmp_path / "a", tmp_path / "b"
    export_jsonl(conn, a)
    export_jsonl(conn, b)
    assert (a / "observations.jsonl").read_text(encoding="utf-8") == \
           (b / "observations.jsonl").read_text(encoding="utf-8")

"""`imports restore` — the undo for `imports retract`, which was one-way.

The trap it has to avoid: status alone cannot say why an observation is
retracted. Retracting an import hides its rows, but mirroring also hides a
row of an applied import — a message deleted on the phone that a journal
entry cites, kept for provenance. A restore that brought back "every
retracted row of the import" would resurrect what the owner deleted. So
retract records exactly what it hid, and restore puts back that and nothing
else.
"""

import json

import pytest
from conftest import FIXTURES, cli_project, golden_draft
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs, reconcile
from lifecrm.domain.util import DomainError
from test_doctor import _ai_chat_scan, _seed
from test_reconcile import _ingest

runner = CliRunner()


def _apply(conn, scan) -> dict:
    result = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    return result


def _status_by_id(conn) -> dict[str, str]:
    return {r["external_id"]: r["status"] for r in conn.execute(
        "SELECT external_id, status FROM observations")}


def _handle_counts(conn) -> dict[str, int]:
    return {f"{r['kind']}:{r['value']}": r["observation_count"] for r in conn.execute(
        "SELECT kind, value, observation_count FROM entity_handles")}


def _deleted_at_source(scan, external_id: str) -> dict:
    """The same export a day later, after one message was deleted from it."""
    return dict(scan,
                records=[r for r in scan["records"] if r["external_id"] != external_id],
                present_ids=[i for i in scan["present_ids"] if i != external_id])


def _cite(conn, external_id: str) -> None:
    """A confirmed entry citing an observation as its provenance."""
    entry_id = conn.execute("SELECT id FROM entries LIMIT 1").fetchone()["id"]
    obs_id = conn.execute("SELECT id FROM observations WHERE external_id=?",
                          (external_id,)).fetchone()["id"]
    conn.execute("INSERT INTO entry_observations (entry_id, observation_id) "
                 "VALUES (?, ?)", (entry_id, obs_id))
    conn.commit()


def _mirror_retracted_m1(conn, cfg, tmp_path) -> tuple[dict, dict]:
    """m1 cited by an entry, then deleted at the source: mirroring hides it
    while its import stays applied."""
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    scan = _ai_chat_scan(cfg, tmp_path)
    first = _apply(conn, scan)
    _cite(conn, "m1")
    second = _apply(conn, _deleted_at_source(scan, "m1"))
    assert second.get("retracted_cited") == 1
    assert _status_by_id(conn)["m1"] == "retracted"
    assert obs.import_by_uuid(conn, first["import_uuid"])["status"] == "applied"
    return scan, first


# ---------------------------------------------------------------- the round trip

def test_restore_puts_back_exactly_what_retract_hid(conn, cfg, tmp_path):
    result = _apply(conn, _ai_chat_scan(cfg, tmp_path))
    before = _handle_counts(conn)
    assert sum(before.values()) > 0

    obs.retract_import(conn, result["import_uuid"])
    conn.commit()
    assert set(_status_by_id(conn).values()) == {"retracted"}
    assert sum(_handle_counts(conn).values()) == 0

    out = obs.restore_import(conn, result["import_uuid"])
    conn.commit()
    assert out == {"restored": 3, "connector": "ai_chat",
                   "source_file": "conversations.json", "note": None}
    assert set(_status_by_id(conn).values()) == {"active"}
    assert _handle_counts(conn) == before                  # the queue is ranked again
    assert obs.import_by_uuid(conn, result["import_uuid"])["status"] == "applied"
    row = conn.execute("SELECT actor, details FROM audit_log WHERE action='restore' "
                       "AND table_name='imports'").fetchone()
    assert row["actor"] == "user"
    assert json.loads(row["details"])["observations_restored"] == 3
    assert run_checks(conn, cfg)["failures"] == 0


def test_retract_restore_retract_restore_uses_the_latest_retraction(conn, cfg, tmp_path):
    result = _apply(conn, _ai_chat_scan(cfg, tmp_path))
    for _ in range(2):
        obs.retract_import(conn, result["import_uuid"])
        assert obs.restore_import(conn, result["import_uuid"])["restored"] == 3
    conn.commit()
    assert set(_status_by_id(conn).values()) == {"active"}


def test_retract_records_what_it_hid_compactly(conn, cfg, tmp_path):
    result = _apply(conn, _ai_chat_scan(cfg, tmp_path))
    obs.retract_import(conn, result["import_uuid"])
    details = json.loads(conn.execute(
        "SELECT details FROM audit_log WHERE action='retract' AND table_name='imports'"
    ).fetchone()["details"])
    ids = sorted(r["id"] for r in conn.execute("SELECT id FROM observations"))
    assert details["observation_id_ranges"] == [[ids[0], ids[-1]]]
    assert details["observations_retracted"] == 3


@pytest.mark.parametrize("ids, ranges", [
    ([], []),
    ([7], [[7, 7]]),
    ([3, 4, 5, 9], [[3, 5], [9, 9]]),
    ([9, 3, 5, 4, 11, 10], [[3, 5], [9, 11]]),
])
def test_id_ranges_are_exact(ids, ranges):
    assert obs._id_ranges(ids) == ranges


# ---------------------------------------------------------------- refusals

def test_restoring_an_import_that_is_not_retracted_is_refused(conn, cfg, tmp_path):
    result = _apply(conn, _ai_chat_scan(cfg, tmp_path))
    with pytest.raises(DomainError) as e:
        obs.restore_import(conn, result["import_uuid"])
    assert str(e.value) == (f"import {result['import_uuid']} is 'applied' — only a "
                            f"retracted import can be restored")


def test_restoring_an_unknown_import_is_refused(conn):
    with pytest.raises(DomainError, match="no import with uuid nope"):
        obs.restore_import(conn, "nope")


# ---------------------------------------------------------------- mirroring

def test_a_message_deleted_at_the_source_stays_hidden_through_retract_and_restore(
        conn, cfg, tmp_path):
    _, first = _mirror_retracted_m1(conn, cfg, tmp_path)

    assert obs.retract_import(conn, first["import_uuid"])["retracted"] == 2
    out = obs.restore_import(conn, first["import_uuid"])
    conn.commit()
    assert out["restored"] == 2
    assert _status_by_id(conn) == {"m1": "retracted", "m2": "active", "m5": "active"}
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_retraction_recorded_before_ranges_is_reconstructed_conservatively(
        conn, cfg, tmp_path):
    """A database whose retract predates the recorded ranges: every retracted
    row except what a mirror_remove audit names."""
    _, first = _mirror_retracted_m1(conn, cfg, tmp_path)
    obs.retract_import(conn, first["import_uuid"])
    # exactly the audit row the one-way retract used to write
    conn.execute("UPDATE audit_log SET details=json_remove(details, "
                 "'$.observation_id_ranges') WHERE action='retract'")
    out = obs.restore_import(conn, first["import_uuid"])
    conn.commit()
    assert out["restored"] == 2
    assert _status_by_id(conn)["m1"] == "retracted"


def test_a_later_import_while_retracted_is_named_in_a_note(conn, cfg, tmp_path):
    """Mirroring only looks at active rows: a run of the same account while
    this import was retracted could not remove anything from it."""
    scan = _ai_chat_scan(cfg, tmp_path)
    first = _apply(conn, scan)
    obs.retract_import(conn, first["import_uuid"])
    conn.commit()
    later = _apply(conn, scan)
    out = obs.restore_import(conn, first["import_uuid"])
    assert out["note"].startswith(f"ai_chat import {later['import_uuid'][:8]} ran on ")
    assert "re-run observe" in out["note"]


def test_doctor_stays_green_after_imports_restore(conn, cfg, tmp_path):
    _seed(conn, cfg)
    result = _apply(conn, _ai_chat_scan(cfg, tmp_path))
    obs.retract_import(conn, result["import_uuid"])
    obs.restore_import(conn, result["import_uuid"])
    conn.commit()
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [(c["check"], c["detail"])
                                     for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- the CLI

def test_cli_retract_points_at_restore_and_restore_undoes_it(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    export = root / "conversations.json"
    export.write_bytes((FIXTURES / "ai_chat_claude.json").read_bytes())
    r = runner.invoke(app, ["observe", "ai_chat", str(export), "--apply", "--json"])
    assert r.exit_code == 0, r.output
    uuid = json.loads(r.output)["applied"]["import_uuid"]

    r = runner.invoke(app, ["imports", "retract", uuid])
    assert r.exit_code == 0, r.output
    assert f"put them back with: python -m lifecrm imports restore {uuid}" in r.output

    r = runner.invoke(app, ["imports", "restore", uuid])
    assert r.exit_code == 0, r.output
    assert r.output.startswith(f"restored 3 observation(s) from ai_chat import {uuid[:8]}")
    assert "note:" not in r.output

    r = runner.invoke(app, ["imports", "restore", uuid])
    assert r.exit_code == 1
    assert "only a retracted import can be restored" in r.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0

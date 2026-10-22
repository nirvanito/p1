"""3.2 — the tests that were missing: an archive that covers every table, and
old databases that come back at the current schema.

The export test used to walk the export's own manifest, so a new table the
export forgot was invisible; it now starts from sqlite_master. The migration
paths were tested at the db layer only — never through `restore` and
`migrate`, the two commands a person actually runs on an old database.
"""

import json
import re
from pathlib import Path

from conftest import cli_project, golden_draft, init_v1_db
from typer.testing import CliRunner

from lifecrm import SCHEMA_VERSION
from lifecrm.cli import app
from lifecrm.domain import entities, observations as obs, reconcile
from lifecrm.export import export_jsonl
from test_doctor import _ai_chat_scan, _op_health_import
from test_reconcile import _ingest

runner = CliRunner()

# Full-text indexes and their shadow tables are rebuilt from content tables;
# an archive carries the content.
_FTS = re.compile(r"_fts(_(config|data|docsize|idx|content))?$")
# Folded into its parent row rather than exported as a table of integer ids.
EMBEDDED = {"extraction_run_sources": "extraction_runs.source_uuids"}

APPLIED = ", ".join(str(v) for v in range(2, SCHEMA_VERSION + 1))


def _every_table(conn) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'") if not _FTS.search(r["name"])}


# ---------------------------------------------------------------- export

def test_export_covers_every_table_in_the_database(conn, cfg, tmp_path):
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    scan = _ai_chat_scan(cfg, tmp_path)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    obs.link_handle(conn, "app_account", "claude:acct-1",
                    entities.create(conn, "person", "Casey")["id"])
    _op_health_import(conn, cfg, tmp_path)
    conn.commit()

    manifest = export_jsonl(conn, tmp_path / "archive")
    expected = _every_table(conn) - set(EMBEDDED)
    assert set(manifest["tables"]) == expected, (
        f"missing from the export: {sorted(expected - set(manifest['tables']))}; "
        f"exported but not a table: {sorted(set(manifest['tables']) - expected)}")


def test_run_sources_survive_inside_extraction_runs(conn, cfg, tmp_path):
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    conn.commit()
    out = tmp_path / "archive"
    export_jsonl(conn, out)
    runs = [json.loads(line) for line in
            (out / "extraction_runs.jsonl").read_text(encoding="utf-8").splitlines()]
    in_db = conn.execute("SELECT COUNT(*) c FROM extraction_run_sources").fetchone()["c"]
    assert in_db > 0
    assert sum(len(r["source_uuids"]) for r in runs) == in_db


# ---------------------------------------------------------------- old databases

def test_a_v1_snapshot_restored_through_the_cli_lands_at_the_current_schema(
        tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    init_v1_db(tmp_path / "v1.db").close()

    r = runner.invoke(app, ["restore", str(tmp_path / "v1.db")])
    assert r.exit_code == 0, r.output
    assert f"applied migrations: {APPLIED}" in r.output
    assert f"(schema v{SCHEMA_VERSION}, 0 entries)" in r.output
    assert list((root / "backups").glob("pre-migration-*.db"))
    assert list((root / "backups").glob("pre-restore-*.db"))
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.output


def test_migrate_gates_writes_then_applies_once(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    (root / "data").mkdir()
    init_v1_db(root / "data" / "lifecrm.db").close()

    r = runner.invoke(app, ["categories", "add", "gaming"])
    assert r.exit_code == 1
    assert "database schema v1 is behind code" in r.output
    assert "run: python -m lifecrm migrate" in r.output

    r = runner.invoke(app, ["migrate"])
    assert r.exit_code == 0, r.output
    m = re.search(rf"applied migrations: {APPLIED} \(pre-migration snapshot: (.+)\)",
                  r.output)
    assert m, r.output
    snap = Path(m.group(1).strip())
    assert snap.exists() and snap.parent == root / "backups"
    assert snap.name.startswith("pre-migration-")

    r = runner.invoke(app, ["migrate"])
    assert r.exit_code == 0, r.output
    assert f"schema v{SCHEMA_VERSION} — up to date" in r.output
    assert runner.invoke(app, ["categories", "add", "gaming"]).exit_code == 0
    assert runner.invoke(app, ["doctor"]).exit_code == 0

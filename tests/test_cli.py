import json
import subprocess

import pytest
from conftest import PROJECT_ROOT, cli_project, golden_draft, sample_text
from typer.testing import CliRunner

from lifecrm.cli import app

runner = CliRunner()


def _sql_json(query: str):
    r = runner.invoke(app, ["sql", query, "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def _setup(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def _ingest_golden(root):
    draft = golden_draft()
    draft_path = root / "data" / "staging" / "draft-2026-07-27.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    r = runner.invoke(app, ["ingest", str(draft_path)])
    assert r.exit_code == 0, r.output
    diffs = sorted((root / "data" / "staging").glob("diff-2026-07-27-*.json"))
    assert diffs, "ingest produced no diff file"
    r = runner.invoke(app, ["apply", str(diffs[-1])])
    assert r.exit_code == 0, r.output
    return diffs[-1]


def test_end_to_end_paste_flow(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)

    rows = _sql_json("SELECT COUNT(*) AS c FROM entries WHERE status='active'")
    assert rows[0]["c"] == 6
    rows = _sql_json("SELECT COUNT(*) AS c FROM measurements WHERE journal_date='2026-07-27'")
    assert rows[0]["c"] == 3
    rows = _sql_json(
        "SELECT snippet(entries_fts, 0, '[', ']', '...', 8) AS s "
        "FROM entries_fts WHERE entries_fts MATCH 'burrito'")
    assert rows and "[" in rows[0]["s"]
    # audit trail exists
    rows = _sql_json("SELECT COUNT(*) AS c FROM audit_log")
    assert rows[0]["c"] > 0


def test_import_command_and_status(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    t = tmp_path / "note.txt"
    t.write_text(sample_text(), encoding="utf-8")
    r = runner.invoke(app, ["import", str(t), "--date", "2026-07-26",
                            "--type", "phone_transcript"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["status", "--json"])
    assert r.exit_code == 0, r.output
    status = json.loads(r.output)
    assert len(status["unprocessed_sources"]) == 1
    assert status["unprocessed_sources"][0]["journal_date"] == "2026-07-26"
    assert status["unprocessed_sources"][0]["type"] == "phone_transcript"


def test_ingest_rejects_invalid_draft(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    draft = golden_draft()
    draft["entries"][0]["categories"]["primary"] = "nonsense"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(draft), encoding="utf-8")
    r = runner.invoke(app, ["ingest", str(p)])
    assert r.exit_code == 1
    # nothing was persisted
    assert _sql_json("SELECT COUNT(*) AS c FROM sources")[0]["c"] == 0


def test_ingest_dry_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    draft = golden_draft()
    p = tmp_path / "draft.json"
    p.write_text(json.dumps(draft), encoding="utf-8")
    r = runner.invoke(app, ["ingest", str(p), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "valid" in r.output
    assert _sql_json("SELECT COUNT(*) AS c FROM sources")[0]["c"] == 0


def test_sql_is_read_only(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["sql", "DELETE FROM categories"])
    assert r.exit_code == 1
    assert _sql_json("SELECT COUNT(*) AS c FROM categories")[0]["c"] == 15


def test_entity_commands(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)

    r = runner.invoke(app, ["entities", "match", "Jake", "Dana", "nobody-at-all"])
    assert r.exit_code == 0, r.output
    results = json.loads(r.output)
    assert results[0]["status"] == "exact"
    assert results[1]["status"] == "exact"
    assert results[2]["status"] == "none"

    # merge Jake into a canonical 'Jake Reilly'
    jake_id = _sql_json("SELECT id FROM entities WHERE name='Jake'")[0]["id"]
    r = runner.invoke(app, ["sql",
                            "SELECT COUNT(*) AS c FROM mentions WHERE entity_id="
                            + str(jake_id), "--json"])
    mentions_before = json.loads(r.output)[0]["c"]
    assert mentions_before >= 1

    from lifecrm import db as db_mod
    conn = db_mod.connect(root / "data" / "lifecrm.db")
    from lifecrm.domain import entities as em
    reilly = em.create(conn, "person", "Jake Reilly")
    conn.commit()
    conn.close()

    r = runner.invoke(app, ["entities", "merge", str(jake_id), str(reilly["id"])])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["entities", "match", "Jake"])
    m = json.loads(r.output)[0]
    assert m["status"] == "alias"
    assert m["matches"][0]["name"] == "Jake Reilly"

    r = runner.invoke(app, ["entities", "alias-add", str(reilly["id"]), "Jaik"])
    assert r.exit_code == 0
    m = json.loads(runner.invoke(app, ["entities", "match", "Jaik"]).output)[0]
    assert m["status"] == "alias"


def test_entry_correction_commands(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    uuid = _sql_json("SELECT uuid FROM entries WHERE title LIKE 'Lunch%'")[0]["uuid"]

    r = runner.invoke(app, ["entry", "edit", uuid, "--title",
                            "Lunch: carnitas burrito bowl", "--secondary", "social"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["entry", "show", uuid, "--json"])
    snap = json.loads(r.output)
    assert snap["title"] == "Lunch: carnitas burrito bowl"
    assert snap["categories"]["secondary"] == ["social"]
    assert [rev["change_kind"] for rev in snap["revisions"]] == ["create", "edit"]

    r = runner.invoke(app, ["entry", "retract", uuid, "--reason", "test"])
    assert r.exit_code == 0
    assert _sql_json(f"SELECT status FROM entries WHERE uuid='{uuid}'")[0]["status"] \
        == "retracted"
    r = runner.invoke(app, ["entry", "restore", uuid])
    assert r.exit_code == 0
    assert _sql_json(f"SELECT status FROM entries WHERE uuid='{uuid}'")[0]["status"] \
        == "active"

    # merge two food entries
    uuids = [r["uuid"] for r in _sql_json(
        "SELECT uuid FROM entries WHERE title IN "
        "('Breakfast: scrambled eggs and toast', 'Dinner: leftover pasta')")]
    r = runner.invoke(app, ["entry", "merge", uuids[0], uuids[1]])
    assert r.exit_code == 0, r.output
    assert _sql_json(f"SELECT status FROM entries WHERE uuid='{uuids[0]}'")[0]["status"] \
        == "superseded"


def test_entry_show_provenance_excerpt(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    uuid = _sql_json("SELECT uuid FROM entries WHERE title LIKE 'Breakfast%'")[0]["uuid"]
    r = runner.invoke(app, ["entry", "show", uuid, "--json"])
    snap = json.loads(r.output)
    assert snap["provenance"], "expected provenance rows"
    excerpt = snap["provenance"][0].get("transcript_excerpt", "")
    assert excerpt and excerpt in sample_text()


def test_backup_restore_round_trip(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    assert _sql_json("SELECT COUNT(*) AS c FROM entries")[0]["c"] == 6

    r = runner.invoke(app, ["backup"])
    assert r.exit_code == 0, r.output
    local = sorted((root / "backups").glob("lifecrm-*.db"))
    cloud = sorted((root / "onedrive" / "backups").glob("lifecrm-*.db"))
    assert len(local) == 1 and len(cloud) == 1

    r = runner.invoke(app, ["admin", "purge-day", "2026-07-27", "--i-am-sure"])
    assert r.exit_code == 0, r.output
    assert _sql_json("SELECT COUNT(*) AS c FROM entries")[0]["c"] == 0

    r = runner.invoke(app, ["restore", str(local[0])])
    assert r.exit_code == 0, r.output
    assert _sql_json("SELECT COUNT(*) AS c FROM entries")[0]["c"] == 6
    # the pre-restore state was itself snapshotted
    assert list((root / "backups").glob("pre-restore-*.db"))


def test_restore_rejects_non_database(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database", encoding="utf-8")
    r = runner.invoke(app, ["restore", str(junk)])
    assert r.exit_code == 1


def test_purge_day_requires_flag(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    r = runner.invoke(app, ["admin", "purge-day", "2026-07-27"])
    assert r.exit_code == 1
    assert _sql_json("SELECT COUNT(*) AS c FROM entries")[0]["c"] == 6


def test_categories_and_kinds_extensible(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert runner.invoke(app, ["categories", "add", "gaming"]).exit_code == 0
    assert runner.invoke(app, ["kinds", "add", "dream"]).exit_code == 0
    r = runner.invoke(app, ["categories", "list"])
    assert "gaming" in r.output
    r = runner.invoke(app, ["kinds", "list"])
    assert "dream" in r.output
    # duplicate rejected
    assert runner.invoke(app, ["categories", "add", "gaming"]).exit_code == 1


def test_gitignore_keeps_private_data_out_of_git():
    """Seed sentinel files under data/ and backups/ in the REAL project and
    assert git never sees them."""
    if not (PROJECT_ROOT / ".git").exists():
        pytest.skip("project is not a git repository")
    probes = [PROJECT_ROOT / "data" / "_privacy_probe.txt",
              PROJECT_ROOT / "data" / "_privacy_probe.db",
              PROJECT_ROOT / "backups" / "_privacy_probe.db",
              PROJECT_ROOT / "_privacy_probe.jsonl"]
    try:
        for p in probes:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("private", encoding="utf-8")
        out = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, check=True).stdout
        for p in probes:
            assert p.name not in out, f"{p} is visible to git — .gitignore is broken"
    finally:
        for p in probes:
            if p.exists():
                p.unlink()

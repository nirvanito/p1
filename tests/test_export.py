import json

from conftest import golden_draft

from lifecrm.domain import corrections, links, reconcile
from lifecrm.export import export_jsonl, export_markdown
from test_reconcile import _ingest


def _seed(conn, cfg):
    """A day through the real pipeline plus a correction, a closure, a link."""
    diff = _ingest(conn, cfg, golden_draft())
    reconcile.apply_diff(conn, diff)
    lunch = conn.execute("SELECT uuid FROM entries WHERE title LIKE 'Lunch%'"
                         ).fetchone()["uuid"]
    corrections.edit_entry(conn, lunch, {"title": "Lunch: carnitas burrito bowl"})
    idea = conn.execute("SELECT uuid FROM entries WHERE title LIKE 'App to%'"
                        ).fetchone()["uuid"]
    corrections.close_entry(conn, idea, "obsolete", note="superseded by this CRM")
    boulder = conn.execute("SELECT uuid FROM entries WHERE title LIKE 'Bouldering%'"
                           ).fetchone()["uuid"]
    links.add(conn, idea, boulder, "relates")
    conn.commit()


def _read_jsonl(path):
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines()]


def test_jsonl_round_trip(conn, cfg, tmp_path):
    _seed(conn, cfg)
    out = tmp_path / "export"
    manifest = export_jsonl(conn, out)

    # every advertised table exists with exactly the advertised row count,
    # and that count matches the database
    for table, count in manifest["tables"].items():
        rows = _read_jsonl(out / f"{table}.jsonl")
        assert len(rows) == count, table
        db_count = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        assert count == db_count, table

    entries = _read_jsonl(out / "entries.jsonl")
    by_title = {e["title"]: e for e in entries}
    assert by_title["Lunch: carnitas burrito bowl"]["kind"] == "event"
    closed = by_title["App to track how often I call Mom"]
    assert closed["closure"] == "obsolete" and closed["closed_at"]
    # ids are uuids/names, never integers
    assert all(isinstance(e["uuid"], str) for e in entries)
    assert "kind_id" not in entries[0] and "id" not in entries[0]

    mentions = _read_jsonl(out / "mentions.jsonl")
    assert all(m["entry_uuid"] and m["entity_uuid"] for m in mentions)
    link_rows = _read_jsonl(out / "entry_links.jsonl")
    assert link_rows[0]["type"] == "relates"

    segs = _read_jsonl(out / "transcript_segments.jsonl")
    assert segs and all("source_uuid" in s for s in segs)

    assert manifest["schema_version"] == 3
    assert manifest["contract_version"] == 2


def test_jsonl_deterministic(conn, cfg, tmp_path):
    _seed(conn, cfg)
    a, b = tmp_path / "a", tmp_path / "b"
    ma = export_jsonl(conn, a)
    mb = export_jsonl(conn, b)
    for table in ma["tables"]:
        assert (a / f"{table}.jsonl").read_bytes() == \
               (b / f"{table}.jsonl").read_bytes(), table
    # manifests identical except the timestamp
    ja = json.loads((a / "manifest.json").read_text(encoding="utf-8"))
    jb = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    ja.pop("exported_at"), jb.pop("exported_at")
    assert ja == jb


def test_markdown_export(conn, cfg, tmp_path):
    _seed(conn, cfg)
    out = tmp_path / "md"
    result = export_markdown(conn, out)
    assert result["files"] > 0

    day = (out / "2026" / "2026-07-27.md").read_text(encoding="utf-8")
    assert day.startswith("---\ndate: 2026-07-27\n")
    assert "mood: 7" in day
    assert "## exercise" in day
    assert "Bouldering at Movement with Jake" in day
    assert "[[Jake]]" in day and "[[Movement]]" in day
    assert "closed: obsolete" in day
    assert "relates ->" in day        # link rendered on the from side

    jake = (out / "_entities" / "jake.md").read_text(encoding="utf-8")
    assert "# Jake" in jake and "type: person" in jake
    assert "2026-07-27" in jake


def test_markdown_deterministic(conn, cfg, tmp_path):
    _seed(conn, cfg)
    a, b = tmp_path / "a", tmp_path / "b"
    export_markdown(conn, a)
    export_markdown(conn, b)
    files_a = sorted(p.relative_to(a) for p in a.rglob("*.md"))
    files_b = sorted(p.relative_to(b) for p in b.rglob("*.md"))
    assert files_a == files_b
    for rel in files_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def test_markdown_excludes_retracted(conn, cfg, tmp_path):
    _seed(conn, cfg)
    dinner = conn.execute("SELECT uuid FROM entries WHERE title LIKE 'Dinner%'"
                          ).fetchone()["uuid"]
    corrections.retract_entry(conn, dinner, reason="test")
    conn.commit()
    out = tmp_path / "md"
    export_markdown(conn, out)
    day = (out / "2026" / "2026-07-27.md").read_text(encoding="utf-8")
    assert "Dinner" not in day

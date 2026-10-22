"""Relationships at the surfaces: `entities link ...`, `entities show`, the
ingest/apply summaries, and both export formats."""

import json

from test_cli import _ingest_golden, _setup, _sql_json, runner
from test_graph_contract import relationship_draft

from lifecrm import db as db_mod
from lifecrm.cli import app
from lifecrm.domain import entities, entity_links, reconcile
from lifecrm.export import export_jsonl, export_markdown
from test_reconcile import _ingest


def _id(name):
    return _sql_json(f"SELECT id FROM entities WHERE name='{name}' "
                     f"AND status='active'")[0]["id"]


def _invoke(*args):
    r = runner.invoke(app, list(args))
    assert r.exit_code == 0, r.output
    return r.output


# ---------------------------------------------------------------- CLI

def test_entities_link_add_list_show_rm_restore(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    jake, movement = str(_id("Jake")), str(_id("Movement"))

    out = _invoke("entities", "link", "add", jake, movement, "--type", "member_of",
                  "--note", "climbs there weekly", "--since", "2025-01-10")
    assert out.startswith("linked: Jake member_of Movement")
    uuid = out.strip().rsplit("(", 1)[1].rstrip(")")
    assert "already known: Jake member_of Movement" in _invoke(
        "entities", "link", "add", jake, movement, "--type", "member_of")

    listed = _invoke("entities", "link", "list", jake)
    assert f"{uuid[:8]}  Jake member_of Movement" in listed
    assert "climbs there weekly" in listed and "2025-01-10" in listed
    rows = json.loads(_invoke("entities", "link", "list", movement, "--json"))
    assert [(r["direction"], r["other"]["name"]) for r in rows] == [("in", "Jake")]
    assert json.loads(_invoke("entities", "link", "list", "--json"))[0]["uuid"] == uuid

    shown = _invoke("entities", "show", movement)
    assert "relationships:\n  - Jake member_of Movement  (climbs there weekly)" in shown
    detail = json.loads(_invoke("entities", "show", jake, "--json"))
    assert detail["relationships"][0]["text"] == "Jake member_of Movement"

    assert "retracted: Jake member_of Movement" in _invoke(
        "entities", "link", "rm", uuid[:8], "--reason", "test")
    assert "(no relationships)" in _invoke("entities", "link", "list", jake)
    assert "retracted" in _invoke("entities", "link", "list", jake, "--all")
    assert "restored: Jake member_of Movement" in _invoke(
        "entities", "link", "restore", uuid)
    assert _sql_json("SELECT action FROM audit_log WHERE table_name='entity_links' "
                     "ORDER BY id") == [{"action": "create"}, {"action": "retract"},
                                        {"action": "restore"}]


def test_entities_link_errors_are_one_line_and_write_nothing(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    jake, dana = str(_id("Jake")), str(_id("Dana"))
    for args, fragment in (
            ((jake, dana, "--type", "bestie"), "unknown relationship type 'bestie'"),
            ((jake, jake, "--type", "knows"), "cannot connect an entity to itself"),
            ((jake, "999", "--type", "knows"), "entity id=999 does not exist"),
            ((jake, dana, "--type", "knows", "--since", "spring"),
             "valid_from 'spring' is not an ISO date")):
        r = runner.invoke(app, ["entities", "link", "add", *args])
        assert r.exit_code == 1, r.output
        assert fragment in r.output and r.output.count("error:") == 1
    r = runner.invoke(app, ["entities", "link", "rm", "deadbeef"])
    assert r.exit_code == 1 and "relationship deadbeef does not exist" in r.output
    assert _sql_json("SELECT COUNT(*) c FROM entity_links")[0]["c"] == 0


def test_link_types_lists_the_seeds_and_type_add_extends_them(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    types = _invoke("entities", "link", "types")
    assert "parent_of" in types and "[inverse: child_of]" in types
    assert "partner_of" in types and "[symmetric]" in types
    out = _invoke("entities", "link", "type-add", "reports_to", "--description",
                  "FROM reports to TO", "--inverse", "manages")
    assert "relationship type 'reports_to' added" in out
    names = [t["name"] for t in json.loads(_invoke("entities", "link", "types", "--json"))]
    assert "reports_to" in names
    dana, jake = str(_id("Dana")), str(_id("Jake"))
    _invoke("entities", "link", "add", jake, dana, "--type", "manages")
    assert "already known: Jake manages Dana" in _invoke(
        "entities", "link", "add", dana, jake, "--type", "reports_to")


def test_ingest_and_apply_report_relationships(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    draft_path = root / "data" / "staging" / "draft-2026-07-28.json"
    draft_path.write_text(json.dumps(relationship_draft(), ensure_ascii=False),
                          encoding="utf-8")
    out = _invoke("ingest", str(draft_path))
    assert "0 link(s), 4 relationship(s), " in out
    diff_path = sorted((root / "data" / "staging").glob("diff-2026-07-28-*.json"))[-1]
    out = _invoke("apply", str(diff_path))
    assert "4 relationship(s), 5 new entities" in out
    jake = str(_id("Jake Morales"))
    assert "Jake Morales from El Paso" in _invoke("entities", "show", jake)
    doctor = json.loads(_invoke("doctor", "--json"))
    assert doctor["failures"] == 0


def test_merge_on_the_cli_carries_relationships(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    jake, movement = str(_id("Jake")), str(_id("Movement"))
    _invoke("entities", "link", "add", jake, movement, "--type", "member_of")
    conn = db_mod.connect(root / "data" / "lifecrm.db")
    try:
        reilly = entities.create(conn, "person", "Jake Reilly")
        conn.commit()
    finally:
        conn.close()
    _invoke("entities", "merge", jake, str(reilly["id"]))
    assert "Jake Reilly member_of Movement" in _invoke("entities", "show", movement)


# ---------------------------------------------------------------- export

def _seed_graph(conn, cfg):
    diff = _ingest(conn, cfg, relationship_draft())
    reconcile.apply_diff(conn, diff)
    jake = conn.execute("SELECT id FROM entities WHERE name='Jake Morales'"
                        ).fetchone()["id"]
    austin = entities.create(conn, "place", "Austin, TX")
    entity_links.add(conn, jake, austin["id"], "lives_in", valid_from="2016-08-15")
    old = entity_links.add(conn, jake, austin["id"], "knows")
    entity_links.retract(conn, old["uuid"], reason="test: retracted rows export too")
    conn.commit()
    return jake, austin


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_jsonl_export_covers_relationships_with_stable_ids(conn, cfg, tmp_path):
    _seed_graph(conn, cfg)
    out = tmp_path / "export"
    manifest = export_jsonl(conn, out)
    assert manifest["schema_version"] == 5
    for table in ("entity_link_types", "entity_links"):
        assert table in manifest["tables"], f"{table} is missing from the export"
        db_count = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        assert manifest["tables"][table] == db_count == \
            len(_read_jsonl(out / f"{table}.jsonl"))
    links = _read_jsonl(out / "entity_links.jsonl")
    assert len(links) == 6 and {r["status"] for r in links} == {"active", "retracted"}
    entity_uuids = {r["uuid"] for r in _read_jsonl(out / "entities.jsonl")}
    source_uuids = {r["uuid"] for r in _read_jsonl(out / "sources.jsonl")}
    for r in links:
        assert r["from_entity_uuid"] in entity_uuids and r["to_entity_uuid"] in entity_uuids
        assert "from_entity_id" not in r and "source_id" not in r
    extracted = [r for r in links if r["actor"] == "extraction"]
    assert len(extracted) == 4
    assert all(r["source_uuid"] in source_uuids and r["extraction_run_uuid"]
               for r in extracted)
    types = {r["name"]: r for r in _read_jsonl(out / "entity_link_types.jsonl")}
    assert types["partner_of"]["symmetric"] is True
    assert types["child_of"]["inverse"] == "parent_of"


def test_jsonl_export_of_relationships_is_deterministic(conn, cfg, tmp_path):
    _seed_graph(conn, cfg)
    a, b = tmp_path / "a", tmp_path / "b"
    export_jsonl(conn, a)
    export_jsonl(conn, b)
    for table in ("entity_link_types", "entity_links"):
        assert (a / f"{table}.jsonl").read_bytes() == (b / f"{table}.jsonl").read_bytes()


def test_markdown_entity_pages_list_relationships_both_ways(conn, cfg, tmp_path):
    _seed_graph(conn, cfg)
    out = tmp_path / "md"
    export_markdown(conn, out)
    jake = (out / "_entities" / "jake-morales.md").read_text(encoding="utf-8")
    assert "## relationships" in jake
    assert "- [[Jake Morales]] from [[El Paso]] — grew up there" in jake
    assert "- [[Jake Morales]] works_at [[Acme Robotics]]" in jake
    assert "- [[Jake Morales]] lives_in [[Austin, TX]] (since 2016-08-15)" in jake
    assert "knows" not in jake                               # retracted stays out
    el_paso = (out / "_entities" / "el-paso.md").read_text(encoding="utf-8")
    assert "- [[Jake Morales]] from [[El Paso]] — grew up there" in el_paso
    # Austin is in no entry — known only through the relationship — and
    # still gets a page, so the wiki link leads somewhere
    austin = (out / "_entities" / "austin-tx.md").read_text(encoding="utf-8")
    assert austin.startswith("# Austin, TX\n\ntype: place\n\n## relationships\n")
    assert "\n\n\n" not in austin


def test_markdown_export_with_relationships_is_deterministic(conn, cfg, tmp_path):
    _seed_graph(conn, cfg)
    a, b = tmp_path / "a", tmp_path / "b"
    export_markdown(conn, a)
    export_markdown(conn, b)
    files = sorted(p.relative_to(a) for p in a.rglob("*.md"))
    assert files == sorted(p.relative_to(b) for p in b.rglob("*.md"))
    for rel in files:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel

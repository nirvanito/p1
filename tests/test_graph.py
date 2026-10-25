"""Entity <-> entity relationships (schema v5): the migration, the domain
layer, merge repointing, and the doctor checks that keep them honest.

The people are the demo persona's, so "Jake from El Paso" here is the same
fact the demo's journal states."""

import shutil
import sqlite3

import pytest
from conftest import FIXTURES, golden_draft, init_v1_db

from lifecrm import SCHEMA_VERSION
from lifecrm import db as db_mod
from lifecrm.connectors import ai_chat
from lifecrm.demo import persona
from lifecrm.doctor import run_checks
from lifecrm.domain import corrections, entities, entity_links, observations as obs
from lifecrm.domain import reconcile
from lifecrm.domain.util import DomainError, now_utc
from lifecrm.importer import import_text
from test_db import _normalized_master
from test_doctor import _seed
from test_reconcile import _ingest

SEEDED = {"knows", "friend_of", "partner_of", "family_of", "parent_of",
          "child_of", "sibling_of", "colleague_of", "manages", "works_at",
          "member_of", "lives_in", "from", "located_in", "part_of", "relates"}
SYMMETRIC = {"knows", "friend_of", "partner_of", "family_of", "sibling_of",
             "colleague_of", "relates"}


def _people(conn) -> dict:
    """The persona's cast as entities: owner, best friend, partner, mother,
    manager, plus the places and employer their relationships need."""
    ids = {}
    for key in ("me", "jake", "maya", "mom", "priya"):
        p = persona.person(key)
        ids[key] = entities.create(conn, "person", p.name)["id"]
    ids["el_paso"] = entities.create(conn, "place", persona.person("jake").hometown)["id"]
    ids["austin"] = entities.create(conn, "place", persona.OWNER.city)["id"]
    ids["acme"] = entities.create(conn, "organization", persona.OWNER.org)["id"]
    return ids


def _status(report, name):
    return next(c for c in report["checks"] if c["check"] == name)["status"]


def _v4_db(cfg, tmp_path):
    """A database exactly as v4 code left it: v1 plus migrations 0002-0004."""
    conn = init_v1_db(cfg.db_path)
    mig = tmp_path / "migrations_v4"
    mig.mkdir()
    for f in sorted(db_mod.MIGRATIONS_DIR.glob("000[234]_*.sql")):
        shutil.copy(f, mig / f.name)
    assert db_mod.migrate(conn, migrations_dir=mig) == [2, 3, 4]
    return conn


# ---------------------------------------------------------------- migration

def test_schema_version_is_six():
    assert SCHEMA_VERSION == 6


def test_migration_0005_takes_a_v4_database_to_v5_and_keeps_its_data(cfg, tmp_path):
    conn = _v4_db(cfg, tmp_path)
    try:
        jake = entities.create(conn, "person", "Jake Morales")
        el_paso = entities.create(conn, "place", "El Paso, TX")
        conn.commit()
        snap = cfg.backups_dir / "pre-migration-v5.db"
        assert db_mod.migrate(conn, snapshot_to=snap) == [5, 6]
        assert db_mod.schema_version(conn) == SCHEMA_VERSION
        assert conn.execute("SELECT value FROM meta WHERE key='contract_version'"
                            ).fetchone()["value"] == "2"   # backwards-compatible key
        names = {r["name"] for r in conn.execute("SELECT name FROM entity_link_types")}
        assert names == SEEDED
        # the v4 rows are untouched and immediately linkable
        made = entity_links.add(conn, jake["id"], el_paso["id"], "from")
        assert made["created"] and made["text"] == "Jake Morales from El Paso, TX"
        conn.commit()
        assert run_checks(conn)["failures"] == 0
    finally:
        conn.close()
    old = sqlite3.connect(str(snap))
    try:
        assert old.execute("SELECT value FROM meta WHERE key='schema_version'"
                           ).fetchone()[0] == "4"
    finally:
        old.close()


def test_v4_plus_0005_matches_a_fresh_init_schema_and_seed_rows(cfg, tmp_path):
    conn = _v4_db(cfg, tmp_path)
    fresh_path = tmp_path / "fresh.db"
    db_mod.init_db(fresh_path, now_utc())
    fresh = db_mod.connect(fresh_path)
    try:
        db_mod.migrate(conn)
        assert _normalized_master(conn) == _normalized_master(fresh)
        q = ("SELECT name, symmetric, inverse, description FROM entity_link_types "
             "ORDER BY name")
        assert [tuple(r) for r in conn.execute(q)] == [tuple(r) for r in fresh.execute(q)]
    finally:
        conn.close()
        fresh.close()


def test_the_v1_chain_reaches_v5_with_the_relationship_tables(v1_conn):
    applied = db_mod.migrate(v1_conn)
    assert applied[-1] == SCHEMA_VERSION == db_mod.schema_version(v1_conn)
    v1_conn.execute("SELECT * FROM entity_links LIMIT 0")
    assert v1_conn.execute("SELECT COUNT(*) c FROM entity_link_types"
                           ).fetchone()["c"] == len(SEEDED)


def test_schema_sql_carries_migration_0005_verbatim():
    """schema.sql's v5 section is the migration minus its header paragraph —
    seed rows included — so the two paths cannot drift."""
    def norm(text):
        return text.replace("\r\n", "\n")
    mig = norm((db_mod.MIGRATIONS_DIR / "0005_entity_links.sql")
               .read_text(encoding="utf-8-sig"))
    body = mig.split("\n\n", 1)[1]
    assert body in norm(db_mod.SCHEMA_FILE.read_text(encoding="utf-8"))


def test_fresh_init_seeds_the_documented_types(conn):
    types = {t["name"]: t for t in entity_links.link_types(conn)}
    assert set(types) == SEEDED
    assert {n for n, t in types.items() if t["symmetric"]} == SYMMETRIC
    assert types["parent_of"]["inverse"] == "child_of"
    assert types["child_of"]["inverse"] == "parent_of"
    assert all(t["description"].startswith("FROM ") for t in types.values())


# ---------------------------------------------------------------- domain

def test_add_creates_an_audited_link_that_reads_as_a_sentence(conn):
    ids = _people(conn)
    made = entity_links.add(conn, ids["jake"], ids["el_paso"], "from",
                            note="grew up there")
    assert made["created"] is True
    assert made["text"] == "Jake Morales from El Paso, TX"
    row = conn.execute("SELECT * FROM entity_links WHERE uuid=?",
                       (made["uuid"],)).fetchone()
    assert row["status"] == "active" and row["actor"] == "user"
    assert row["confirmed_at"] and row["note"] == "grew up there"
    audit = conn.execute("SELECT * FROM audit_log WHERE table_name='entity_links' "
                         "AND row_uuid=?", (made["uuid"],)).fetchone()
    assert audit["action"] == "create"


def test_adding_the_same_fact_again_returns_the_existing_link(conn):
    ids = _people(conn)
    first = entity_links.add(conn, ids["priya"], ids["acme"], "works_at")
    again = entity_links.add(conn, ids["priya"], ids["acme"], "works_at",
                             note="said twice")
    assert again == {"uuid": first["uuid"], "created": False, "text": first["text"]}
    assert conn.execute("SELECT COUNT(*) c FROM entity_links").fetchone()["c"] == 1


def test_symmetric_types_are_stored_once_in_canonical_order(conn):
    ids = _people(conn)
    assert ids["maya"] > ids["me"]
    made = entity_links.add(conn, ids["maya"], ids["me"], "partner_of")
    row = conn.execute("SELECT from_entity_id, to_entity_id FROM entity_links "
                       "WHERE uuid=?", (made["uuid"],)).fetchone()
    assert (row["from_entity_id"], row["to_entity_id"]) == (ids["me"], ids["maya"])
    other_reading = entity_links.add(conn, ids["me"], ids["maya"], "partner_of")
    assert other_reading["created"] is False and other_reading["uuid"] == made["uuid"]


def test_an_inverse_pair_is_one_fact(conn):
    ids = _people(conn)
    made = entity_links.add(conn, ids["mom"], ids["me"], "parent_of")
    from_the_child = entity_links.add(conn, ids["me"], ids["mom"], "child_of")
    assert from_the_child["created"] is False
    assert from_the_child["uuid"] == made["uuid"]


def test_a_directed_type_is_not_symmetric(conn):
    ids = _people(conn)
    entity_links.add(conn, ids["priya"], ids["me"], "manages")
    reverse = entity_links.add(conn, ids["me"], ids["priya"], "manages")
    assert reverse["created"] is True    # a different (if unlikely) fact


@pytest.mark.parametrize("case, fragment", [
    ("self", "cannot connect an entity to itself"),
    ("unknown_type", "unknown relationship type 'bestie' (expected one of: child_of"),
    ("missing_entity", "entity id=999 does not exist"),
    ("merged_entity", "was merged into"),
    ("bad_date", "valid_from 'last spring' is not an ISO date"),
    ("dates_backwards", "valid_from 2024-05-01 is after valid_to 2023-01-01"),
    ("extraction_bare", "an extracted relationship needs provenance"),
    ("span_without_source", "a character span needs the source it indexes"),
    ("span_past_text", "exceeds transcript length"),
    ("half_span", "char_start and char_end go together"),
    ("missing_entry", "entry nope does not exist"),
    ("missing_observation", "observation nope does not exist"),
])
def test_add_refuses_with_one_line(conn, cfg, case, fragment):
    ids = _people(conn)
    src = import_text(conn, cfg, "Jake grew up in El Paso.", journal_date="2026-07-27")
    jake, el_paso = ids["jake"], ids["el_paso"]
    kw: dict = {}
    if case == "self":
        el_paso = jake
    elif case == "unknown_type":
        kw["link_type"] = "bestie"
    elif case == "missing_entity":
        el_paso = 999
    elif case == "merged_entity":
        dup = entities.create(conn, "place", "El Paso")
        entities.merge(conn, dup["id"], ids["el_paso"])
        el_paso = dup["id"]
    elif case == "bad_date":
        kw["valid_from"] = "last spring"
    elif case == "dates_backwards":
        kw.update(valid_from="2024-05-01", valid_to="2023-01-01")
    elif case == "extraction_bare":
        kw["actor"] = "extraction"
    elif case == "span_without_source":
        kw.update(char_start=0, char_end=5)
    elif case == "span_past_text":
        kw.update(source_id=src["source_id"], char_start=0, char_end=500)
    elif case == "half_span":
        kw.update(source_id=src["source_id"], char_start=0)
    elif case == "missing_entry":
        kw["entry_uuid"] = "nope"
    elif case == "missing_observation":
        kw["observation_uuid"] = "nope"
    link_type = kw.pop("link_type", "from")
    with pytest.raises(DomainError) as e:
        entity_links.add(conn, jake, el_paso, link_type, **kw)
    assert fragment in str(e.value) and "\n" not in str(e.value)


def test_an_extracted_link_citing_its_passage_is_accepted(conn, cfg):
    ids = _people(conn)
    text = "Jake grew up in El Paso."
    src = import_text(conn, cfg, text, journal_date="2026-07-27")
    made = entity_links.add(conn, ids["jake"], ids["el_paso"], "from",
                            source_id=src["source_id"], char_start=0,
                            char_end=len(text), actor="extraction")
    row = conn.execute("SELECT * FROM entity_links WHERE uuid=?",
                       (made["uuid"],)).fetchone()
    assert (row["source_id"], row["char_start"], row["char_end"]) == \
        (src["source_id"], 0, len(text))


def test_retract_hides_the_link_and_restore_brings_it_back(conn):
    ids = _people(conn)
    made = entity_links.add(conn, ids["jake"], ids["austin"], "lives_in")
    entity_links.retract(conn, made["uuid"], reason="moved")
    assert entity_links.for_entity(conn, ids["jake"]) == []
    hidden = entity_links.for_entity(conn, ids["jake"], include_retracted=True)
    assert [r["status"] for r in hidden] == ["retracted"]
    with pytest.raises(DomainError, match="already retracted"):
        entity_links.retract(conn, made["uuid"])
    # retracted, the fact may be stated afresh; restoring the old row is then refused
    fresh = entity_links.add(conn, ids["jake"], ids["austin"], "lives_in")
    assert fresh["created"] and fresh["uuid"] != made["uuid"]
    with pytest.raises(DomainError, match="the same relationship is already active"):
        entity_links.restore(conn, made["uuid"])
    entity_links.retract(conn, fresh["uuid"])
    entity_links.restore(conn, made["uuid"])
    assert [r["uuid"] for r in entity_links.for_entity(conn, ids["jake"])] == [made["uuid"]]
    with pytest.raises(DomainError, match="is not retracted"):
        entity_links.restore(conn, made["uuid"])
    actions = [r["action"] for r in conn.execute(
        "SELECT action FROM audit_log WHERE row_uuid=? ORDER BY id", (made["uuid"],))]
    assert actions == ["create", "retract", "restore"]


def test_for_entity_reads_each_link_from_the_entitys_side(conn):
    ids = _people(conn)
    entity_links.add(conn, ids["jake"], ids["el_paso"], "from")
    entity_links.add(conn, ids["jake"], ids["me"], "friend_of", note="best friend")
    entity_links.add(conn, ids["mom"], ids["jake"], "knows")
    entity_links.add(conn, ids["priya"], ids["jake"], "manages")

    jake = {r["type"]: r for r in entity_links.for_entity(conn, ids["jake"])}
    assert jake["from"]["direction"] == "out"
    assert jake["from"]["other"]["name"] == "El Paso, TX"
    assert jake["friend_of"]["direction"] == "both"
    assert jake["friend_of"]["other"]["name"] == "Alex Rivera"
    assert jake["manages"]["direction"] == "in"
    assert jake["manages"]["text"] == "Priya Natarajan manages Jake Morales"
    # "where is Jake from" — the read recall needs
    hometown = entity_links.for_entity(conn, ids["jake"], types=("from",))
    assert [(r["direction"], r["other"]["name"]) for r in hometown] == \
        [("out", "El Paso, TX")]
    # El Paso's side of the same fact
    assert [r["direction"] for r in entity_links.for_entity(conn, ids["el_paso"])] == ["in"]


def test_get_accepts_a_unique_prefix_and_refuses_unknown(conn):
    ids = _people(conn)
    made = entity_links.add(conn, ids["jake"], ids["el_paso"], "from")
    assert entity_links.get(conn, made["uuid"][:8])["uuid"] == made["uuid"]
    with pytest.raises(DomainError, match="does not exist"):
        entity_links.get(conn, "00000000")


def test_list_links_filters_by_type(conn):
    ids = _people(conn)
    entity_links.add(conn, ids["jake"], ids["el_paso"], "from")
    entity_links.add(conn, ids["priya"], ids["acme"], "works_at")
    assert [r["type"] for r in entity_links.list_links(conn)] == ["from", "works_at"]
    assert [r["text"] for r in entity_links.list_links(conn, link_type="works_at")] == \
        ["Priya Natarajan works_at Acme Robotics"]
    with pytest.raises(DomainError, match="unknown relationship type"):
        entity_links.list_links(conn, link_type="nope")
    with pytest.raises(DomainError, match="unknown relationship type 'hometown'"):
        entity_links.for_entity(conn, ids["jake"], types=("hometown",))


def test_add_type_pairs_an_inverse_and_the_new_type_works(conn):
    ids = _people(conn)
    entity_links.add_type(conn, "reports_to", "FROM reports to TO", inverse="manages")
    types = {t["name"]: t for t in entity_links.link_types(conn)}
    assert types["reports_to"]["inverse"] == "manages"
    assert types["manages"]["inverse"] == "reports_to"
    made = entity_links.add(conn, ids["priya"], ids["me"], "manages")
    assert entity_links.add(conn, ids["me"], ids["priya"], "reports_to")["uuid"] == made["uuid"]
    assert run_checks(conn)["failures"] == 0


@pytest.mark.parametrize("kw, fragment", [
    ({"name": "Reports To"}, "must be lowercase snake_case"),
    ({"name": "knows"}, "already exists"),
    ({"description": "  "}, "needs a description"),
    ({"symmetric": True, "inverse": "manages"}, "not both"),
    ({"inverse": "knows"}, "'knows' is symmetric"),
    ({"inverse": "parent_of"}, "already has an inverse ('child_of')"),
    ({"inverse": "nope"}, "unknown relationship type 'nope'"),
])
def test_add_type_refuses_with_one_line(conn, kw, fragment):
    args = {"name": "mentors", "description": "FROM mentors TO"} | kw
    name, description = args.pop("name"), args.pop("description")
    with pytest.raises(DomainError) as e:
        entity_links.add_type(conn, name, description, **args)
    assert fragment in str(e.value)
    assert conn.execute("SELECT COUNT(*) c FROM entity_link_types").fetchone()["c"] == \
        len(SEEDED)


# ---------------------------------------------------------------- merge

def test_merge_repoints_relationships_to_the_survivor(conn):
    ids = _people(conn)
    jakey = entities.create(conn, "person", "Jakey")
    link = entity_links.add(conn, jakey["id"], ids["el_paso"], "from")
    result = entities.merge(conn, jakey["id"], ids["jake"])
    assert result["links_moved"] == 1 and result["links_retracted"] == 0
    row = conn.execute("SELECT from_entity_id FROM entity_links WHERE uuid=?",
                       (link["uuid"],)).fetchone()
    assert row["from_entity_id"] == ids["jake"]
    assert [r["text"] for r in entity_links.for_entity(conn, ids["jake"])] == \
        ["Jake Morales from El Paso, TX"]
    assert run_checks(conn)["failures"] == 0


def test_merge_retracts_what_would_become_a_duplicate_or_a_self_link(conn):
    ids = _people(conn)
    jakey = entities.create(conn, "person", "Jakey")
    keep = entity_links.add(conn, ids["jake"], ids["el_paso"], "from")
    dup = entity_links.add(conn, jakey["id"], ids["el_paso"], "from")
    self_link = entity_links.add(conn, jakey["id"], ids["jake"], "knows")
    result = entities.merge(conn, jakey["id"], ids["jake"])
    assert result["links_retracted"] == 2
    status = {r["uuid"]: r["status"] for r in conn.execute(
        "SELECT uuid, status FROM entity_links")}
    assert status == {keep["uuid"]: "active", dup["uuid"]: "retracted",
                      self_link["uuid"]: "retracted"}
    reasons = [r["details"] for r in conn.execute(
        "SELECT details FROM audit_log WHERE table_name='entity_links' "
        "AND action='retract' ORDER BY id")]
    assert any(keep["uuid"] in d for d in reasons)        # "duplicate of <keep>"
    assert any("same entity" in d for d in reasons)
    assert run_checks(conn)["failures"] == 0


def test_merge_keeps_symmetric_links_in_canonical_order(conn):
    ids = _people(conn)
    jacob = entities.create(conn, "person", "Jacob Morales")
    assert ids["jake"] < ids["maya"] < jacob["id"]
    friend = entity_links.add(conn, ids["maya"], ids["jake"], "friend_of")
    # Jake (low id) merges into Jacob (high id): the end moves past its
    # partner, so the pair must flip to stay canonical
    entities.merge(conn, ids["jake"], jacob["id"])
    row = conn.execute("SELECT from_entity_id, to_entity_id, status FROM entity_links "
                       "WHERE uuid=?", (friend["uuid"],)).fetchone()
    assert (row["from_entity_id"], row["to_entity_id"]) == (ids["maya"], jacob["id"])
    assert row["status"] == "active"
    assert entity_links.add(conn, jacob["id"], ids["maya"], "friend_of")["created"] is False
    assert _status(run_checks(conn), "entity_links_canonical") == "ok"


def test_a_retracted_link_moves_with_the_merge_and_restores_onto_the_survivor(conn):
    ids = _people(conn)
    jakey = entities.create(conn, "person", "Jakey")
    link = entity_links.add(conn, jakey["id"], ids["austin"], "lives_in")
    entity_links.retract(conn, link["uuid"])
    entities.merge(conn, jakey["id"], ids["jake"])
    entity_links.restore(conn, link["uuid"])
    assert [r["text"] for r in entity_links.for_entity(conn, ids["jake"])] == \
        ["Jake Morales lives_in Austin, TX"]


def test_restore_refuses_a_link_whose_end_was_merged(conn):
    ids = _people(conn)
    jakey = entities.create(conn, "person", "Jakey")
    link = entity_links.add(conn, jakey["id"], ids["jake"], "knows")
    entities.merge(conn, jakey["id"], ids["jake"])   # retracted: would self-link
    with pytest.raises(DomainError, match="was merged into"):
        entity_links.restore(conn, link["uuid"])


# ---------------------------------------------------------------- doctor

def _corrupt(conn, ids, case):
    """Direct SQL is correct here: these are the states the domain layer must
    never produce, so only a test can produce them."""
    now = now_utc()

    def insert(f, t, ltype, actor="user", uid="corrupt-1"):
        conn.execute("INSERT INTO entity_links (uuid, from_entity_id, to_entity_id, "
                     "type, actor, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (uid, f, t, ltype, actor, now, now))
    if case == "merged_end":
        dup = entities.create(conn, "person", "Jakey")
        insert(dup["id"], ids["el_paso"], "from")
        conn.execute("UPDATE entities SET status='merged', merged_into=? WHERE id=?",
                     (ids["jake"], dup["id"]))
        return "entity_links_resolve"
    if case == "non_canonical":
        insert(ids["maya"], ids["me"], "partner_of")
        return "entity_links_canonical"
    if case == "no_provenance":
        insert(ids["jake"], ids["el_paso"], "from", actor="extraction")
        return "entity_links_provenance"
    if case == "both_halves":
        insert(ids["mom"], ids["me"], "parent_of", uid="corrupt-1")
        insert(ids["me"], ids["mom"], "child_of", uid="corrupt-2")
        return "entity_links_inverse_once"
    if case == "one_way_inverse":
        conn.execute("INSERT INTO entity_link_types (name, symmetric, inverse, "
                     "description, created_at) VALUES ('mentors', 0, 'manages', "
                     "'FROM mentors TO', ?)", (now,))
        return "entity_link_types_inverse"
    raise AssertionError(case)


@pytest.mark.parametrize("case", ["merged_end", "non_canonical", "no_provenance",
                                  "both_halves", "one_way_inverse"])
def test_doctor_catches_relationship_corruption(conn, case):
    ids = _people(conn)
    assert run_checks(conn)["failures"] == 0
    check = _corrupt(conn, ids, case)
    report = run_checks(conn)
    assert _status(report, check) == "fail"


def test_doctor_warns_when_a_relationship_came_with_a_retracted_entry(conn, cfg):
    _seed(conn, cfg)
    ids = _people(conn)
    entry = conn.execute("SELECT uuid FROM entries LIMIT 1").fetchone()["uuid"]
    entity_links.add(conn, ids["jake"], ids["el_paso"], "from", entry_uuid=entry)
    assert _status(run_checks(conn), "entity_links_cite_retracted") == "ok"
    corrections.retract_entry(conn, entry, reason="never said that")
    report = run_checks(conn)
    assert _status(report, "entity_links_cite_retracted") == "warn"
    assert report["failures"] == 0


def test_purge_day_keeps_the_relationship_and_its_source_passage(conn, cfg):
    """entry_uuid is SET NULL, not CASCADE: the relationship outlives the
    entry it arrived with, and the passage still carries its provenance."""
    _seed(conn, cfg)
    ids = _people(conn)
    entry = conn.execute("SELECT e.uuid, es.source_id FROM entries e "
                         "JOIN entry_sources es ON es.entry_id=e.id LIMIT 1").fetchone()
    made = entity_links.add(conn, ids["jake"], ids["el_paso"], "from",
                            entry_uuid=entry["uuid"], source_id=entry["source_id"],
                            char_start=0, char_end=10, actor="extraction")
    corrections.purge_day(conn, "2026-07-27")
    conn.commit()
    row = conn.execute("SELECT * FROM entity_links WHERE uuid=?",
                       (made["uuid"],)).fetchone()
    assert row["status"] == "active" and row["entry_uuid"] is None
    assert row["source_id"] == entry["source_id"]
    assert run_checks(conn, cfg)["failures"] == 0


def test_an_import_writes_no_relationships(conn, cfg, tmp_path):
    export = tmp_path / "conversations.json"
    export.write_bytes((FIXTURES / "ai_chat_claude.json").read_bytes())
    scan = ai_chat.scan(export, cfg)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    assert conn.execute("SELECT COUNT(*) c FROM entity_links").fetchone()["c"] == 0


# Every write path keeps doctor green — the same discipline as
# test_doctor.DOMAIN_OPS, for the operations this feature adds.

def _op_add(conn, cfg):
    ids = _people(conn)
    entity_links.add(conn, ids["jake"], ids["el_paso"], "from")


def _op_add_extracted(conn, cfg):
    ids = _people(conn)
    row = conn.execute("SELECT e.uuid, es.source_id FROM entries e "
                       "JOIN entry_sources es ON es.entry_id=e.id LIMIT 1").fetchone()
    entity_links.add(conn, ids["jake"], ids["el_paso"], "from", actor="extraction",
                     source_id=row["source_id"], char_start=0, char_end=20,
                     entry_uuid=row["uuid"])


def _op_retract(conn, cfg):
    ids = _people(conn)
    made = entity_links.add(conn, ids["maya"], ids["me"], "partner_of")
    entity_links.retract(conn, made["uuid"])


def _op_restore(conn, cfg):
    ids = _people(conn)
    made = entity_links.add(conn, ids["mom"], ids["me"], "parent_of")
    entity_links.retract(conn, made["uuid"])
    entity_links.restore(conn, made["uuid"])


def _op_merge(conn, cfg):
    ids = _people(conn)
    jakey = entities.create(conn, "person", "Jakey")
    entity_links.add(conn, jakey["id"], ids["el_paso"], "from")
    entity_links.add(conn, ids["jake"], ids["el_paso"], "from")
    entity_links.add(conn, jakey["id"], ids["jake"], "knows")
    entity_links.add(conn, jakey["id"], ids["me"], "friend_of")
    entities.merge(conn, jakey["id"], ids["jake"])


def _op_type_add(conn, cfg):
    ids = _people(conn)
    entity_links.add_type(conn, "reports_to", "FROM reports to TO", inverse="manages")
    entity_links.add(conn, ids["me"], ids["priya"], "reports_to")


def _op_apply_with_relationships(conn, cfg):
    from test_graph_contract import relationship_draft
    diff = _ingest(conn, cfg, relationship_draft())
    reconcile.apply_diff(conn, diff)


def _op_purge_after_extraction(conn, cfg):
    _op_add_extracted(conn, cfg)
    corrections.purge_day(conn, "2026-07-27")


GRAPH_OPS = {
    "entities link add": _op_add,
    "extracted link": _op_add_extracted,
    "entities link rm": _op_retract,
    "entities link restore": _op_restore,
    "entities merge with links": _op_merge,
    "entities link type-add": _op_type_add,
    "apply with entity_links": _op_apply_with_relationships,
    "purge-day after extraction": _op_purge_after_extraction,
}


@pytest.mark.parametrize("op", list(GRAPH_OPS))
def test_doctor_stays_green_after(op, conn, cfg):
    _seed(conn, cfg)
    assert run_checks(conn, cfg)["failures"] == 0, "seed itself is not green"

    GRAPH_OPS[op](conn, cfg)
    conn.commit()

    report = run_checks(conn, cfg)
    assert report["failures"] == 0, (
        f"{op} broke: " + str([(c["check"], c.get("detail"))
                               for c in report["checks"] if c["status"] == "fail"]))


def test_golden_seed_has_no_relationships(conn, cfg):
    """The seed every doctor test starts from is untouched by v5."""
    diff = _ingest(conn, cfg, golden_draft())
    assert diff["counts"]["entity_links"] == 0
    assert not [i for i in diff["items"] if i["action"] == "entity_link"]

import pytest

from lifecrm.domain import entities
from lifecrm.domain.util import DomainError


def test_match_none(conn):
    assert entities.match(conn, "Nobody")["status"] == "none"


def test_exact_and_alias_match(conn):
    e = entities.create(conn, "person", "Jake Reilly", aliases=["Jake", "Jake from climbing"])
    assert entities.match(conn, "jake reilly")["status"] == "exact"
    m = entities.match(conn, "Jake")
    assert m["status"] == "alias"
    assert m["matches"][0]["id"] == e["id"]


def test_ambiguous_alias_returns_all_candidates(conn):
    a = entities.create(conn, "person", "Jake Reilly", aliases=["Jake"])
    b = entities.create(conn, "person", "Jake Smith", aliases=["Jake"])
    m = entities.match(conn, "Jake")
    assert m["status"] == "alias"
    assert {x["id"] for x in m["matches"]} == {a["id"], b["id"]}


def test_fuzzy_candidates_never_exact(conn):
    entities.create(conn, "place", "Movement Gym")
    m = entities.match(conn, "Movemint Gym")   # whisper-style misspelling
    assert m["status"] == "candidates"
    assert m["matches"][0]["name"] == "Movement Gym"
    assert 0 < m["matches"][0]["score"] <= 1


def test_type_filter(conn):
    entities.create(conn, "person", "Atlas")
    entities.create(conn, "project", "Atlas")
    assert len(entities.match(conn, "Atlas")["matches"]) == 2
    m = entities.match(conn, "Atlas", "project")
    assert len(m["matches"]) == 1
    assert m["matches"][0]["type"] == "project"


def test_duplicate_active_name_rejected(conn):
    entities.create(conn, "person", "Dana")
    with pytest.raises(DomainError, match="already exists"):
        entities.create(conn, "person", "dana")


def test_slug_collision(conn):
    a = entities.create(conn, "person", "Sam Lee")
    b = entities.create(conn, "place", "Sam Lee")   # same slug base, different type
    assert a["slug"] != b["slug"]


def test_merge_moves_mentions_and_aliases(conn):
    from lifecrm.domain import entries
    src = entities.create(conn, "person", "Jake", aliases=["Jaik"])
    dst = entities.create(conn, "person", "Jake Reilly")
    e = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="Climbing", primary_category="exercise")
    entries.attach_entity(conn, e["id"], src["id"], "participant")

    result = entities.merge(conn, src["id"], dst["id"])
    assert result["mentions_moved"] == 1
    # mention now points at dst
    row = conn.execute("SELECT entity_id FROM mentions WHERE entry_id=?",
                       (e["id"],)).fetchone()
    assert row["entity_id"] == dst["id"]
    # src name and its aliases now resolve to dst
    for name in ("Jake", "Jaik"):
        m = entities.match(conn, name)
        assert m["status"] == "alias"
        assert m["matches"][0]["id"] == dst["id"]
    # src kept, marked merged, resolvable
    assert entities.get(conn, src["id"])["status"] == "merged"
    assert entities.resolve_merged(conn, src["id"]) == dst["id"]
    # a fresh 'Jake' person can be created again later (unique among active only)
    entities.create(conn, "person", "Jake")


def test_merge_type_mismatch(conn):
    a = entities.create(conn, "person", "Atlas")
    b = entities.create(conn, "project", "Atlas Project")
    with pytest.raises(DomainError, match="type mismatch"):
        entities.merge(conn, a["id"], b["id"])


def test_merge_self_rejected(conn):
    a = entities.create(conn, "person", "Solo")
    with pytest.raises(DomainError, match="itself"):
        entities.merge(conn, a["id"], a["id"])


def test_merge_writes_audit(conn):
    a = entities.create(conn, "person", "A")
    b = entities.create(conn, "person", "B")
    entities.merge(conn, a["id"], b["id"])
    row = conn.execute(
        "SELECT * FROM audit_log WHERE action='merge' AND table_name='entities'").fetchone()
    assert row is not None
    assert row["row_uuid"] == a["uuid"]


def test_rename_keeps_old_name_as_alias(conn):
    e = entities.create(conn, "person", "Jake")
    entities.rename(conn, e["id"], "Jake Reilly")
    m = entities.match(conn, "Jake")
    assert m["status"] == "alias"
    assert entities.match(conn, "Jake Reilly")["status"] == "exact"


# ---------------------------------------------------------------- word-level

@pytest.mark.parametrize("query,expected", [
    ("Reilly, Jake", "Jake Reilly"),        # reordered
    ("Alex Chen", "Alexander Chen"),        # a longer first name
    ("Dana W.", "Dana Whitfield"),          # an initial
    ("José Núñez", "Jose Nunez"),           # accents on one side only
])
def test_word_level_candidates(conn, query, expected):
    ent = entities.create(conn, "person", expected)
    result = entities.match(conn, query)
    assert result["status"] == "candidates"
    assert result["matches"][0]["id"] == ent["id"]
    assert result["matches"][0]["score"] < 1.0       # never passes for exact


@pytest.mark.parametrize("query,other", [("Sam Ortiz", "Sam Smith"), ("Mom", "Tom")])
def test_word_level_does_not_invent_matches(conn, query, other):
    entities.create(conn, "person", other)
    assert entities.match(conn, query)["status"] == "none"

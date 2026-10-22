"""Relationships through the review gate: the optional `entity_links` key of
a contract-v2 draft, its validation, the `entity_link` diff items, and apply.

A draft states a relationship only where the note does ("Jake grew up in El
Paso" -> Jake from El Paso), citing that sentence; a `new` end must be an
entity some entry of the same draft creates."""

import copy

import pytest
from conftest import golden_draft

from lifecrm import contract
from lifecrm.doctor import run_checks
from lifecrm.domain import entities, entity_links, reconcile
from lifecrm.domain.util import DomainError
from lifecrm.importer import import_text
from test_reconcile import _ingest

DATE = "2026-07-28"
S0 = "Dinner at Veracruz All Natural with Jake Morales, the fish tacos."
S1 = "Jake grew up in El Paso."
S2 = "He works at Acme Robotics with Priya Natarajan."
TEXT = " ".join([S0, S1, S2])


def _span(sentence: str, text: str = TEXT) -> dict:
    start = text.index(sentence)
    return {"char_start": start, "char_end": start + len(sentence)}


def _new(etype: str, name: str, role: str = "mention") -> dict:
    return {"ref": "new", "type": etype, "name": name, "role": role}


def _end(etype: str, name: str) -> dict:
    return {"ref": "new", "type": etype, "name": name}


def relationship_draft() -> dict:
    """One evening note: a dinner, where Jake grew up, where he works."""
    return {
        "contract_version": 2,
        "journal_date": DATE,
        "sources": [{"text": TEXT, "type": "pasted"}],
        "entries": [
            {"kind": "event", "categories": {"primary": "food", "secondary": ["social"]},
             "title": "Fish tacos at Veracruz with Jake", "time_of_day": "evening",
             "entities": [_new("person", "Jake Morales", "participant"),
                          _new("place", "Veracruz All Natural", "location")],
             "provenance": _span(S0)},
            {"kind": "fact", "categories": {"primary": "social"},
             "title": "Jake grew up in El Paso",
             "entities": [_new("person", "Jake Morales", "subject"),
                          _new("place", "El Paso")],
             "provenance": _span(S1)},
            {"kind": "fact", "categories": {"primary": "work"},
             "title": "Jake works at Acme Robotics with Priya",
             "entities": [_new("person", "Jake Morales", "subject"),
                          _new("organization", "Acme Robotics"),
                          _new("person", "Priya Natarajan")],
             "provenance": _span(S2)},
        ],
        "entity_links": [
            {"from": _end("person", "Jake Morales"), "to": _end("place", "El Paso"),
             "type": "from", "note": "grew up there", "entry_index": 1,
             "provenance": _span(S1)},
            {"from": _end("person", "Jake Morales"),
             "to": _end("organization", "Acme Robotics"),
             "type": "works_at", "entry_index": 2, "provenance": _span(S2)},
            {"from": _end("person", "Priya Natarajan"),
             "to": _end("organization", "Acme Robotics"),
             "type": "works_at", "entry_index": 2, "provenance": _span(S2)},
            {"from": _end("person", "Jake Morales"),
             "to": _end("person", "Priya Natarajan"),
             "type": "colleague_of", "provenance": _span(S2)},
        ],
    }


def _errors(conn, draft, inline=TEXT, texts=None):
    return contract.validate_draft(conn, draft, texts or {}, inline)


def _apply(conn, cfg, draft):
    diff = _ingest(conn, cfg, draft)
    summary = reconcile.apply_diff(conn, diff)
    conn.commit()
    return diff, summary


def _entity_id(conn, name):
    return conn.execute("SELECT id FROM entities WHERE name=? AND status='active'",
                        (name,)).fetchone()["id"]


# ---------------------------------------------------------------- validation

def test_a_draft_with_relationships_validates(conn):
    assert _errors(conn, relationship_draft()) == []


def test_a_draft_without_relationships_is_exactly_as_before(conn, cfg):
    draft = golden_draft()
    assert "entity_links" not in draft
    diff = _ingest(conn, cfg, draft)
    assert diff["counts"]["entity_links"] == 0
    assert diff["counts"]["entity_links_unchanged"] == 0
    summary = reconcile.apply_diff(conn, diff)
    assert summary["entity_links_created"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM entity_links").fetchone()["c"] == 0


def _mutate(draft, case):
    links = draft["entity_links"]
    first = links[0]
    if case == "not_array":
        draft["entity_links"] = {"from": first["from"]}
    elif case == "not_object":
        links[0] = "Jake from El Paso"
    elif case == "unknown_key":
        first["confidence"] = "high"
    elif case == "unknown_type":
        first["type"] = "hometown"
    elif case == "end_missing":
        del first["to"]
    elif case == "bad_ref":
        first["to"] = {"ref": "maybe", "name": "El Paso"}
    elif case == "new_not_in_entries":
        first["to"] = _end("place", "Juarez")
    elif case == "new_wrong_type":
        first["to"] = _end("organization", "El Paso")
    elif case == "end_unknown_key":
        first["to"]["aliases"] = ["EP"]
    elif case == "existing_missing":
        first["to"] = {"ref": "existing", "id": 999}
    elif case == "existing_not_int":
        first["to"] = {"ref": "existing", "id": "3"}
    elif case == "self":
        first["to"] = _end("person", "Jake Morales")
    elif case == "duplicate":
        links.append(copy.deepcopy(first))
    elif case == "symmetric_duplicate":
        links.append({"from": _end("person", "Priya Natarajan"),
                      "to": _end("person", "Jake Morales"),
                      "type": "colleague_of", "provenance": _span(S2)})
    elif case == "no_provenance":
        del first["provenance"]
    elif case == "segments":
        first["provenance"] = {"segments": [1]}
    elif case == "span_past_text":
        first["provenance"] = {"char_start": 0, "char_end": len(TEXT) + 5}
    elif case == "entry_index":
        first["entry_index"] = 9
    elif case == "bad_date":
        first["valid_from"] = "since forever"
    elif case == "dates_backwards":
        first.update(valid_from="2001-01-01", valid_to="1999-01-01")
    elif case == "note_type":
        first["note"] = 42
    else:
        raise AssertionError(case)


@pytest.mark.parametrize("case, fragment", [
    ("not_array", "entity_links: must be an array"),
    ("not_object", "entity_links[0]: must be an object"),
    ("unknown_key", "entity_links[0].confidence: unknown key"),
    ("unknown_type", "entity_links[0].type: 'hometown' is not a relationship type"),
    ("end_missing", "entity_links[0].to: required object"),
    ("bad_ref", "entity_links[0].to.ref: must be 'existing' or 'new'"),
    ("new_not_in_entries", "new place 'Juarez' is not a new entity in this draft's entries"),
    ("new_wrong_type", "new organization 'El Paso' is not a new entity"),
    ("end_unknown_key", "entity_links[0].to.aliases: unknown key"),
    ("existing_missing", "entity_links[0].to.id: entity id=999 does not exist"),
    ("existing_not_int", "existing ref requires an integer id"),
    ("self", "entity_links[0]: a relationship cannot connect an entity to itself"),
    ("duplicate", "entity_links[4]: same relationship as entity_links[0]"),
    ("symmetric_duplicate", "entity_links[4]: same relationship as entity_links[3]"),
    ("no_provenance", "entity_links[0].provenance: required"),
    ("segments", "segments are not accepted for relationships"),
    ("span_past_text", "exceeds transcript length"),
    ("entry_index", "entity_links[0].entry_index: 9 is not a valid index"),
    ("bad_date", "entity_links[0].valid_from: 'since forever' is not an ISO"),
    ("dates_backwards", "valid_from 2001-01-01 is after valid_to 1999-01-01"),
    ("note_type", "entity_links[0].note: must be a string or null"),
])
def test_validation_rejects_with_one_line(conn, case, fragment):
    draft = relationship_draft()
    _mutate(draft, case)
    errors = _errors(conn, draft)
    assert any(fragment in e for e in errors), errors
    assert all("\n" not in e for e in errors)


def test_an_existing_end_that_was_merged_is_refused_with_the_survivor(conn):
    jake = entities.create(conn, "person", "Jake Morales")
    jakey = entities.create(conn, "person", "Jakey")
    entities.merge(conn, jakey["id"], jake["id"])
    draft = relationship_draft()
    draft["entity_links"][0]["from"] = {"ref": "existing", "id": jakey["id"]}
    errors = _errors(conn, draft)
    assert any(f"was merged into id={jake['id']} — use that id" in e for e in errors), errors


def test_an_existing_end_of_the_wrong_type_is_refused(conn):
    el_paso = entities.create(conn, "place", "El Paso, TX")
    draft = relationship_draft()
    draft["entity_links"][0]["to"] = {"ref": "existing", "id": el_paso["id"],
                                      "type": "organization"}
    errors = _errors(conn, draft)
    assert any("is a place, not organization" in e for e in errors), errors


def test_a_multi_source_relationship_must_name_its_source(conn, cfg):
    a = import_text(conn, cfg, S0, journal_date=DATE)
    b = import_text(conn, cfg, f"{S1} {S2}", journal_date=DATE)
    draft = relationship_draft()
    draft["sources"] = [{"source_id": a["source_id"]}, {"source_id": b["source_id"]}]
    for i, entry in enumerate(draft["entries"]):
        sid = a["source_id"] if i == 0 else b["source_id"]
        text = S0 if i == 0 else f"{S1} {S2}"
        entry["provenance"] = {"source_id": sid, **_span([S0, S1, S2][i], text)}
    texts = {a["source_id"]: S0, b["source_id"]: f"{S1} {S2}"}
    for link in draft["entity_links"]:
        sentence = S1 if link["type"] == "from" else S2
        link["provenance"] = _span(sentence, f"{S1} {S2}")
    errors = contract.validate_draft(conn, draft, texts, None)
    assert any("entity_links[0].provenance.source_id: required in multi-source drafts"
               in e for e in errors), errors
    for link in draft["entity_links"]:
        link["provenance"]["source_id"] = b["source_id"]
    assert contract.validate_draft(conn, draft, texts, None) == []


# ---------------------------------------------------------------- diff + apply

def test_ingest_carries_relationships_as_reviewable_adds(conn, cfg):
    diff = _ingest(conn, cfg, relationship_draft())
    items = [i for i in diff["items"] if i["action"] == "entity_link"]
    assert [i["text"] for i in items] == [
        "Jake Morales from El Paso", "Jake Morales works_at Acme Robotics",
        "Priya Natarajan works_at Acme Robotics",
        "Jake Morales colleague_of Priya Natarajan"]
    assert all(i["state"] == "add" for i in items)
    assert all("accepted" not in i for i in items)        # adds are not decisions
    assert diff["counts"]["entity_links"] == 4
    assert diff["counts"]["requires_decision"] == 0


def test_apply_creates_relationships_citing_passage_entry_and_run(conn, cfg):
    diff, summary = _apply(conn, cfg, relationship_draft())
    assert summary["entity_links_created"] == 4
    assert summary["entities_created"] == 5   # Jake, Veracruz, El Paso, Acme, Priya

    jake = _entity_id(conn, "Jake Morales")
    hometown = entity_links.for_entity(conn, jake, types=("from",))
    assert [h["other"]["name"] for h in hometown] == ["El Paso"]
    link = conn.execute("SELECT * FROM entity_links WHERE uuid=?",
                        (hometown[0]["uuid"],)).fetchone()
    source_id = diff["source_ids"][0]
    assert link["actor"] == "extraction" and link["confirmed_at"]
    assert (link["source_id"], link["char_start"], link["char_end"]) == \
        (source_id, _span(S1)["char_start"], _span(S1)["char_end"])
    assert link["note"] == "grew up there"
    run_id = conn.execute("SELECT id FROM extraction_runs WHERE uuid=?",
                          (diff["extraction_run_uuid"],)).fetchone()["id"]
    assert link["extraction_run_id"] == run_id
    fact = conn.execute("SELECT title FROM entries WHERE uuid=?",
                        (link["entry_uuid"],)).fetchone()
    assert fact["title"] == "Jake grew up in El Paso"
    # the colleague link named no entry; its passage is its provenance
    colleague = entity_links.for_entity(conn, jake, types=("colleague_of",))[0]
    assert colleague["provenance"]["entry_uuid"] is None
    assert colleague["provenance"]["char_start"] == _span(S2)["char_start"]
    # the passage really says it
    text = conn.execute("SELECT text FROM transcripts WHERE source_id=?",
                        (source_id,)).fetchone()["text"]
    assert text[link["char_start"]:link["char_end"]] == S1
    assert run_checks(conn, cfg)["failures"] == 0


def _second_day(conn, links):
    """A later note restating relationships between entities that now exist."""
    text = "Jake grew up in El Paso, he told Priya. They work together at Acme."
    return {
        "contract_version": 2, "journal_date": "2026-07-29",
        "sources": [{"text": text, "type": "pasted"}],
        "entries": [{"kind": "fact", "categories": {"primary": "social"},
                     "title": "Jake told Priya about El Paso",
                     "entities": [{"ref": "existing",
                                   "id": _entity_id(conn, "Jake Morales")}],
                     "provenance": {"char_start": 0, "char_end": len(text)}}],
        "entity_links": [dict(link, provenance={"char_start": 0, "char_end": len(text)})
                         for link in links],
    }


def test_a_relationship_already_known_is_unchanged_and_not_stored_twice(conn, cfg):
    _apply(conn, cfg, relationship_draft())
    ids = {n: _entity_id(conn, n) for n in ("Jake Morales", "El Paso",
                                             "Priya Natarajan")}
    before = conn.execute("SELECT COUNT(*) c FROM entity_links").fetchone()["c"]
    draft = _second_day(conn, [
        {"from": {"ref": "existing", "id": ids["Jake Morales"]},
         "to": {"ref": "existing", "id": ids["El Paso"]}, "type": "from"},
        # the other reading of a symmetric fact is the same fact
        {"from": {"ref": "existing", "id": ids["Priya Natarajan"]},
         "to": {"ref": "existing", "id": ids["Jake Morales"]}, "type": "colleague_of"},
    ])
    diff = _ingest(conn, cfg, draft)
    items = [i for i in diff["items"] if i["action"] == "entity_link"]
    assert [i["state"] for i in items] == ["unchanged", "unchanged"]
    assert all(i["existing_link_uuid"] for i in items)
    assert diff["counts"]["entity_links_unchanged"] == 2
    summary = reconcile.apply_diff(conn, diff)
    assert summary["entity_links_unchanged"] == 2
    assert summary["entity_links_created"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM entity_links").fetchone()["c"] == before


def test_an_inverse_restatement_is_unchanged(conn, cfg):
    _apply(conn, cfg, relationship_draft())
    linda = entities.create(conn, "person", "Linda Rivera")
    jake = _entity_id(conn, "Jake Morales")
    entity_links.add(conn, linda["id"], jake, "parent_of")   # fiction, for the shape
    draft = _second_day(conn, [
        {"from": {"ref": "existing", "id": jake},
         "to": {"ref": "existing", "id": linda["id"]}, "type": "child_of"}])
    diff = _ingest(conn, cfg, draft)
    (item,) = [i for i in diff["items"] if i["action"] == "entity_link"]
    assert item["state"] == "unchanged"


def test_a_relationship_learned_between_review_and_apply_is_not_doubled(conn, cfg):
    """Ingest said 'add'; by apply the owner had typed the same fact by hand.
    Apply finds it and counts it unchanged instead of failing or doubling."""
    _apply(conn, cfg, relationship_draft())
    ids = {n: _entity_id(conn, n) for n in ("Jake Morales", "Priya Natarajan")}
    draft = _second_day(conn, [
        {"from": {"ref": "existing", "id": ids["Jake Morales"]},
         "to": {"ref": "existing", "id": ids["Priya Natarajan"]}, "type": "knows"}])
    diff = _ingest(conn, cfg, draft)
    entity_links.add(conn, ids["Priya Natarajan"], ids["Jake Morales"], "knows")
    summary = reconcile.apply_diff(conn, diff)
    assert summary["entity_links_created"] == 0
    assert summary["entity_links_unchanged"] == 1


def test_an_end_merged_after_review_attaches_to_the_survivor(conn, cfg):
    _apply(conn, cfg, relationship_draft())
    jake = _entity_id(conn, "Jake Morales")
    jakey = entities.create(conn, "person", "Jakey")
    austin = entities.create(conn, "place", "Austin, TX")
    draft = _second_day(conn, [
        {"from": {"ref": "existing", "id": jakey["id"]},
         "to": {"ref": "existing", "id": austin["id"]}, "type": "lives_in"}])
    diff = _ingest(conn, cfg, draft)
    entities.merge(conn, jakey["id"], jake)
    reconcile.apply_diff(conn, diff)
    assert [r["text"] for r in entity_links.for_entity(conn, jake, types=("lives_in",))] \
        == ["Jake Morales lives_in Austin, TX"]
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_skipped_duplicate_entry_still_anchors_its_relationship(conn, cfg):
    """entry_index resolving to a possible duplicate that stays skipped cites
    the surviving entry — the same rule links between entries follow."""
    _apply(conn, cfg, relationship_draft())
    original = conn.execute("SELECT uuid FROM entries WHERE title='Jake grew up in El Paso'"
                            ).fetchone()["uuid"]
    # the same day re-extracted, with one extra relationship on the fact entry
    draft = relationship_draft()
    for entry in draft["entries"]:
        entry["entities"] = [
            {"ref": "existing", "id": _entity_id(conn, r["name"]), "role": r["role"]}
            for r in entry["entities"]]
    jake, el_paso = _entity_id(conn, "Jake Morales"), _entity_id(conn, "El Paso")
    draft["entity_links"] = [
        {"from": {"ref": "existing", "id": jake}, "to": {"ref": "existing", "id": el_paso},
         "type": "lives_in", "valid_to": "2011-08-01", "entry_index": 1,
         "provenance": _span(S1)}]
    diff = _ingest(conn, cfg, draft)
    assert {i["action"] for i in diff["items"]} >= {"possible_duplicate", "entity_link"}
    reconcile.apply_diff(conn, diff)
    (link,) = entity_links.for_entity(conn, jake, types=("lives_in",))
    assert link["provenance"]["entry_uuid"] == original
    assert link["valid_to"] == "2011-08-01"
    assert run_checks(conn, cfg)["failures"] == 0


def test_apply_refuses_an_extracted_relationship_with_no_passage(conn, cfg):
    """Defense in depth: a hand-edited diff that strips the provenance is
    refused by the domain layer — the apply fails, and the CLI rolls the
    whole transaction back with it."""
    diff = _ingest(conn, cfg, relationship_draft())
    for item in diff["items"]:
        if item["action"] == "entity_link":
            item["provenance"] = {}
            item.pop("entry_index", None)
    with pytest.raises(DomainError, match="needs provenance"):
        reconcile.apply_diff(conn, diff)

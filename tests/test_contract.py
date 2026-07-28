import copy

from conftest import golden_draft

from lifecrm import contract


def _errors(conn, mutate=None):
    draft = golden_draft()
    if mutate:
        mutate(draft)
    srcs = draft.get("sources") or []
    inline = srcs[0].get("text") if len(srcs) == 1 and isinstance(srcs[0], dict) else None
    return contract.validate_draft(conn, draft, None, inline)


def test_golden_fixture_is_valid(conn):
    assert _errors(conn) == []


def test_wrong_contract_version(conn):
    errs = _errors(conn, lambda d: d.update(contract_version=99))
    assert any("contract_version" in e for e in errs)


def test_future_journal_date_rejected(conn):
    errs = _errors(conn, lambda d: d.update(journal_date="2099-01-01"))
    assert any("journal_date" in e and "future" in e for e in errs)


def test_bad_date_format(conn):
    errs = _errors(conn, lambda d: d.update(journal_date="27/07/2026"))
    assert any("journal_date" in e for e in errs)


def test_source_requires_exactly_one(conn):
    errs = _errors(conn, lambda d: d["sources"][0].update(source_id=1))
    assert any("exactly one" in e for e in errs)


def test_missing_source_id(conn):
    def mutate(d):
        d["sources"] = [{"source_id": 12345}]
    errs = _errors(conn, mutate)
    assert any("does not exist" in e for e in errs)


def test_v1_singular_source_key_gets_helpful_error(conn):
    def mutate(d):
        d["source"] = d["sources"][0]
        del d["sources"]
    errs = _errors(conn, mutate)
    assert any("sources" in e and "ARRAY" in e for e in errs)


def test_inline_text_forbidden_in_multi_source_drafts(conn):
    def mutate(d):
        d["sources"].append({"text": "another note"})
    errs = _errors(conn, mutate)
    assert any("inline text is only allowed in single-source" in e for e in errs)


def test_unknown_category(conn):
    errs = _errors(conn, lambda d: d["entries"][0]["categories"].update(primary="foods"))
    assert any("categories.primary" in e and "foods" in e for e in errs)


def test_unknown_secondary_category(conn):
    errs = _errors(conn, lambda d: d["entries"][0]["categories"].update(secondary=["nope"]))
    assert any("secondary" in e for e in errs)


def test_unknown_kind(conn):
    errs = _errors(conn, lambda d: d["entries"][0].update(kind="happening"))
    assert any(".kind" in e for e in errs)


def test_empty_title(conn):
    errs = _errors(conn, lambda d: d["entries"][0].update(title="  "))
    assert any(".title" in e for e in errs)


def test_bad_time_of_day(conn):
    errs = _errors(conn, lambda d: d["entries"][0].update(time_of_day="dawn"))
    assert any("time_of_day" in e for e in errs)


def test_future_due_at_is_valid(conn):
    """Plans and tasks may carry future dates — journal_date stays historical."""
    def mutate(d):
        d["entries"].append({
            "kind": "plan", "categories": {"primary": "travel"},
            "title": "Book flights for October trip", "due_at": "2026-10-01"})
    assert _errors(conn, mutate) == []


def test_unknown_top_level_key(conn):
    errs = _errors(conn, lambda d: d.update(relationships=[]))
    assert any("relationships" in e for e in errs)


def test_unknown_entry_key(conn):
    errs = _errors(conn, lambda d: d["entries"][0].update(mood=7))
    assert any("entries[0].mood" in e for e in errs)


def test_measurement_out_of_range(conn):
    errs = _errors(conn, lambda d: d["day"]["measurements"].append(
        {"metric": "mood", "numeric_value": 15}))
    assert any("mood" in e and "range" in e for e in errs)


def test_measurement_unknown_unit(conn):
    errs = _errors(conn, lambda d: d["entries"][3]["measurements"].append(
        {"metric": "distance", "numeric_value": 5, "unit": "furlongs"}))
    assert any("unknown unit" in e for e in errs)


def test_open_ended_metric_allowed(conn):
    def mutate(d):
        d["day"]["measurements"].append({"metric": "focus_score", "numeric_value": 42})
    assert _errors(conn, mutate) == []


def test_measurement_requires_value(conn):
    errs = _errors(conn, lambda d: d["day"]["measurements"].append({"metric": "mood"}))
    assert any("numeric_value or text_value" in e for e in errs)


def test_new_entity_matching_existing_rejected(conn):
    from lifecrm.domain import entities
    entities.create(conn, "person", "Jake")
    errs = _errors(conn)
    assert any("ref:existing" in e and "Jake" in e for e in errs)


def test_existing_entity_ref_must_exist(conn):
    def mutate(d):
        d["entries"][0]["entities"] = [{"ref": "existing", "id": 999, "role": "mention"}]
    errs = _errors(conn, mutate)
    assert any("id=999 does not exist" in e for e in errs)


def test_existing_ref_to_merged_entity_rejected(conn):
    from lifecrm.domain import entities
    a = entities.create(conn, "person", "Jake")
    b = entities.create(conn, "person", "Jake Reilly")
    entities.merge(conn, a["id"], b["id"])

    def mutate(d):
        d["entries"] = [{"kind": "event", "categories": {"primary": "social"},
                         "title": "Coffee",
                         "entities": [{"ref": "existing", "id": a["id"]}]}]
    errs = _errors(conn, mutate)
    assert any("merged into" in e for e in errs)


def test_bad_entity_role(conn):
    def mutate(d):
        d["entries"][1]["entities"][0]["role"] = "friend"
    errs = _errors(conn, mutate)
    assert any(".role" in e for e in errs)


def test_provenance_char_bounds(conn):
    errs = _errors(conn, lambda d: d["entries"][0].update(
        provenance={"char_start": 0, "char_end": 100000}))
    assert any("exceeds transcript length" in e for e in errs)


def test_provenance_exactly_one_form(conn):
    errs = _errors(conn, lambda d: d["entries"][0].update(
        provenance={"char_start": 0, "char_end": 10, "segments": [1]}))
    assert any("exactly one" in e for e in errs)


def test_uuid_targeting_missing_entry(conn):
    errs = _errors(conn, lambda d: d["entries"][0].update(
        uuid="00000000-0000-0000-0000-000000000000"))
    assert any("does not exist" in e for e in errs)


def test_error_messages_are_single_line(conn):
    draft = golden_draft()
    draft["journal_date"] = "bad"
    draft["entries"][0]["kind"] = "nope"
    draft["entries"][2]["categories"]["primary"] = "nope"
    for e in contract.validate_draft(conn, draft, None):
        assert "\n" not in e

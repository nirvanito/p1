"""Integrity fixes (brief 1.6 and 3.1): provenance that is never clobbered,
an audited day summary, a contract that says everything ingest will refuse,
entries[].uuid held to its own day (decision #5), a way to end a draft run,
and duplicate detection that needs more than a similar title."""

import json

import pytest
from conftest import golden_draft

from lifecrm import contract
from lifecrm.domain import corrections, days, entities, entries, reconcile
from lifecrm.domain.sources import content_hash_text
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks
from lifecrm.importer import import_text
from test_doctor import _seed
from test_reconcile import _ingest, _source_text


def _import(conn, cfg, text, date="2026-07-27"):
    return import_text(conn, cfg, text, journal_date=date)["source_id"]


def _entry(title, kind="event", cat="social", **kw):
    e = {"kind": kind, "categories": {"primary": cat}, "title": title}
    e.update(kw)
    return e


def _draft(source_ids, entries_list, date="2026-07-27", **top):
    d = {"contract_version": 2, "journal_date": date,
         "sources": [{"source_id": s} for s in source_ids],
         "entries": entries_list}
    d.update(top)
    return d


def _errors(conn, draft):
    texts = {s["source_id"]: _source_text(conn, s["source_id"])
             for s in draft["sources"] if "source_id" in s}
    return contract.validate_draft(conn, draft, texts)


def _golden_source(conn):
    return conn.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()["id"]


def _entry_row(conn, title_like):
    return conn.execute("SELECT id, uuid FROM entries WHERE title LIKE ?",
                        (title_like,)).fetchone()


def _close_via_entry_update(conn, cfg, target_uuid, source_id, prov):
    """A same-day entry_update from the source that created the target — the
    one path that writes entry_sources for an entry that already has a row."""
    draft = _draft([source_id], [], entry_updates=[{
        "target_uuid": target_uuid, "set": {"closure": "done"},
        "provenance": {"source_id": source_id, **prov}}])
    diff = _ingest(conn, cfg, draft)
    for item in diff["items"]:
        if item["action"] == "entry_update":
            item["accepted"] = True
    return reconcile.apply_diff(conn, diff)


# ---------------------------------------------------------------- 1.6 provenance

def test_an_entry_update_never_clobbers_the_targets_original_provenance(conn, cfg):
    _seed(conn, cfg)
    sid = _golden_source(conn)
    breakfast = _entry_row(conn, "Breakfast%")
    before = dict(conn.execute(
        "SELECT * FROM entry_sources WHERE entry_id=? AND source_id=?",
        (breakfast["id"], sid)).fetchone())
    assert (before["char_start"], before["char_end"]) == (0, 60)

    summary = _close_via_entry_update(conn, cfg, breakfast["uuid"], sid,
                                      {"char_start": 100, "char_end": 110})
    assert summary["closed"] == 1

    after = dict(conn.execute(
        "SELECT * FROM entry_sources WHERE entry_id=? AND source_id=?",
        (breakfast["id"], sid)).fetchone())
    assert after == before        # passage, run and original JSON all intact
    assert json.loads(after["original_json"])["title"] == \
        "Breakfast: scrambled eggs and toast"


def test_a_row_without_a_passage_gains_one_and_keeps_its_original_json(conn, cfg):
    _seed(conn, cfg)
    sid = _golden_source(conn)
    lunch = _entry_row(conn, "Lunch%")       # extracted without provenance offsets
    _close_via_entry_update(conn, cfg, lunch["uuid"], sid,
                            {"char_start": 0, "char_end": 10})
    row = conn.execute("SELECT * FROM entry_sources WHERE entry_id=?",
                       (lunch["id"],)).fetchone()
    assert (row["char_start"], row["char_end"]) == (0, 10)
    assert json.loads(row["original_json"])["title"] == "Lunch: chicken burrito bowl"


def test_a_passage_is_kept_whole_rather_than_mixed_with_a_second_one(conn, cfg):
    """Segments on one call, char offsets on the next: the row must not end
    up claiming both, which would describe neither passage."""
    _seed(conn, cfg)
    sid = _golden_source(conn)
    boulder = _entry_row(conn, "Bouldering%")   # provenance: segments [4, 5]
    entries.add_provenance(conn, entry_id=boulder["id"], source_id=sid,
                           char_start=0, char_end=5)
    row = conn.execute("SELECT * FROM entry_sources WHERE entry_id=?",
                       (boulder["id"],)).fetchone()
    assert (row["segment_start"], row["segment_end"]) == (4, 5)
    assert (row["char_start"], row["char_end"]) == (None, None)


# ---------------------------------------------------------------- 1.6 summary audit

def _summary_audits(conn):
    return [(r["actor"], json.loads(r["details"])) for r in conn.execute(
        "SELECT actor, details FROM audit_log WHERE action='set_summary' ORDER BY id")]


def test_setting_a_day_summary_writes_an_audit_row(conn, cfg):
    _seed(conn, cfg)
    ((actor, details),) = _summary_audits(conn)
    assert actor == "extraction"
    assert details["journal_date"] == "2026-07-27"
    assert details["old"] is None and "quarterly report" in details["new"]


def test_an_accepted_summary_conflict_is_audited_as_the_users_decision(conn, cfg):
    _seed(conn, cfg)
    original = conn.execute("SELECT summary FROM days").fetchone()["summary"]
    draft = _draft([_golden_source(conn)], [], day={"summary": "A quieter day."})
    diff = _ingest(conn, cfg, draft)
    assert diff["day"]["summary"]["action"] == "conflict"
    diff["day"]["summary"]["accepted"] = True
    reconcile.apply_diff(conn, diff)

    actor, details = _summary_audits(conn)[-1]
    assert actor == "user"
    assert (details["old"], details["new"]) == (original, "A quieter day.")


def test_an_unchanged_summary_writes_nothing(conn, cfg):
    _seed(conn, cfg)
    summary = conn.execute("SELECT summary FROM days").fetchone()["summary"]
    assert days.set_summary(conn, "2026-07-27", summary) is False
    assert len(_summary_audits(conn)) == 1


# ---------------------------------------------------------------- 3.1 extraction block

@pytest.mark.parametrize("extraction, expected", [
    ("x", "extraction: must be an object"),
    (["claude_skill"], "extraction: must be an object"),
    ({"interface": "foo"}, "extraction.interface: 'foo' must be claude_skill or manual"),
    ({"interface": "manual", "temperature": 0}, "extraction.temperature: unknown key"),
    ({"provider": 7}, "extraction.provider: must be a non-empty string"),
    ({"provider": "x", "model": 3}, "extraction.model: must be a string or null"),
])
def test_the_extraction_block_is_part_of_the_contract(conn, extraction, expected):
    draft = golden_draft()
    draft["extraction"] = extraction
    errs = contract.validate_draft(conn, draft, inline_text=draft["sources"][0]["text"])
    assert any(e.startswith(expected) for e in errs), errs
    assert all("\n" not in e for e in errs)


def test_a_well_formed_extraction_block_is_valid(conn):
    draft = golden_draft()
    draft["extraction"] = {"provider": "claude-code", "model": None,
                           "interface": "claude_skill"}
    assert contract.validate_draft(conn, draft,
                                   inline_text=draft["sources"][0]["text"]) == []


# ---------------------------------------------------------------- 3.1 entries[].uuid

def test_a_same_day_active_uuid_is_still_a_valid_target(conn, cfg):
    _seed(conn, cfg)
    lunch = _entry_row(conn, "Lunch%")
    draft = _draft([_golden_source(conn)],
                   [_entry("Lunch: chicken burrito bowl", cat="food", uuid=lunch["uuid"])])
    assert _errors(conn, draft) == []


def test_a_cross_day_uuid_is_refused_and_points_at_the_explicit_paths(conn, cfg):
    _seed(conn, cfg)
    lunch = _entry_row(conn, "Lunch%")
    s2 = _import(conn, cfg, "Next day, re-thinking yesterday's lunch.", date="2026-07-28")
    draft = _draft([s2], [_entry("Lunch: burrito bowl", cat="food", uuid=lunch["uuid"])],
                   date="2026-07-28")
    errs = _errors(conn, draft)
    assert any("is on 2026-07-27, not 2026-07-28" in e and "entry_updates" in e
               and "lifecrm entry edit" in e for e in errs), errs


def test_a_retracted_uuid_is_refused(conn, cfg):
    _seed(conn, cfg)
    lunch = _entry_row(conn, "Lunch%")
    corrections.retract_entry(conn, lunch["uuid"], reason="never happened")
    draft = _draft([_golden_source(conn)],
                   [_entry("Lunch: chicken burrito bowl", cat="food", uuid=lunch["uuid"])])
    assert any("is retracted — only active entries" in e for e in _errors(conn, draft))


def test_one_uuid_targeted_twice_is_refused(conn, cfg):
    _seed(conn, cfg)
    lunch = _entry_row(conn, "Lunch%")
    draft = _draft([_golden_source(conn)], [
        _entry("Lunch: chicken burrito bowl", cat="food", uuid=lunch["uuid"]),
        _entry("Lunch: burrito bowl, second take", cat="food", uuid=lunch["uuid"])])
    assert any("targeted by more than one entry" in e for e in _errors(conn, draft))


def test_reconcile_refuses_a_cross_day_uuid_that_skipped_the_contract(conn, cfg):
    """Defense in depth: build_diff used to look the entry up anywhere and
    auto-edit it, outside the fingerprint that guards the review."""
    _seed(conn, cfg)
    lunch = _entry_row(conn, "Lunch%")
    s2 = _import(conn, cfg, "Another day.", date="2026-07-28")
    draft = _draft([s2], [_entry("Lunch", cat="food", uuid=lunch["uuid"])],
                   date="2026-07-28")
    run = reconcile.create_extraction_run(
        conn, source_ids=[s2], provider="test", model=None, interface="manual",
        contract_version=2, input_hash=content_hash_text("x"))
    with pytest.raises(DomainError, match="cross-day changes go through entry_updates"):
        reconcile.build_diff(conn, draft, run)


def test_a_uuid_target_is_never_also_matched_as_a_fuzzy_duplicate(conn, cfg):
    _seed(conn, cfg)
    lunch = _entry_row(conn, "Lunch%")
    draft = _draft([_golden_source(conn)], [
        _entry("Lunch: chicken burrito bowl", cat="food"),     # same title, no uuid
        _entry("Lunch: chicken burrito bowl", cat="food", uuid=lunch["uuid"])])
    diff = _ingest(conn, cfg, draft)
    actions = {i["index"]: i["action"] for i in diff["items"] if "index" in i}
    assert actions == {0: "add", 1: "unchanged"}


# ---------------------------------------------------------------- 3.1 discard-run

def _draft_run(conn, cfg):
    """A golden re-ingest left in draft, the way a reviewed-and-rejected diff
    sits in staging."""
    _seed(conn, cfg)
    return _ingest(conn, cfg, golden_draft())


def test_discarding_a_run_ends_it_and_apply_refuses_it(conn, cfg):
    diff = _draft_run(conn, cfg)
    run_uuid = diff["extraction_run_uuid"]
    result = reconcile.discard_run(conn, run_uuid[:8], reason="wrong draft")
    assert result["uuid"] == run_uuid
    assert result["journal_dates"] == ["2026-07-27"]

    row = conn.execute("SELECT status, reviewed_at FROM extraction_runs WHERE uuid=?",
                       (run_uuid,)).fetchone()
    assert row["status"] == "discarded" and row["reviewed_at"]
    audit = conn.execute("SELECT actor, details FROM audit_log WHERE action='discard' "
                         "AND row_uuid=?", (run_uuid,)).fetchone()
    assert audit["actor"] == "user"
    assert json.loads(audit["details"])["reason"] == "wrong draft"

    with pytest.raises(DomainError, match="was discarded — re-run ingest"):
        reconcile.apply_diff(conn, diff)
    with pytest.raises(DomainError, match="already discarded"):
        reconcile.discard_run(conn, run_uuid)


def test_a_discarded_runs_source_is_simply_extracted_again(conn, cfg):
    sid = _import(conn, cfg, "Only note of the day. Called Mom.")
    wrong = _ingest(conn, cfg, _draft([sid], [_entry("Called Tom")]))
    reconcile.discard_run(conn, wrong["extraction_run_uuid"])

    fixed = _ingest(conn, cfg, _draft([sid], [_entry("Called Mom")]))
    assert reconcile.apply_diff(conn, fixed)["added"] == 1
    statuses = sorted(r["status"] for r in conn.execute(
        "SELECT r.status FROM extraction_runs r JOIN extraction_run_sources rs "
        "ON rs.extraction_run_id=r.id WHERE rs.source_id=?", (sid,)))
    assert statuses == ["applied", "discarded"]     # the wrong draft stays on record


def test_an_applied_run_cannot_be_discarded(conn, cfg):
    _seed(conn, cfg)
    run_uuid = conn.execute("SELECT uuid FROM extraction_runs").fetchone()["uuid"]
    with pytest.raises(DomainError, match="was applied — discarding cannot undo"):
        reconcile.discard_run(conn, run_uuid)


def test_run_lookup_refuses_short_unknown_and_ambiguous_prefixes(conn, cfg):
    sid = _import(conn, cfg, "A note.")
    runs = [reconcile.create_extraction_run(
        conn, source_ids=[sid], provider="test", model=None, interface="manual",
        contract_version=2, input_hash="h") for _ in range(2)]
    # Simulate the 1-in-4-billion collision the 8-character prefix allows.
    for n, run in enumerate(runs):
        conn.execute("UPDATE extraction_runs SET uuid=? WHERE id=?",
                     (f"abcdef12-0000-4000-8000-00000000000{n}", run["id"]))
    with pytest.raises(DomainError, match="too short"):
        reconcile.find_run(conn, "abcd")
    with pytest.raises(DomainError, match="no extraction run matches"):
        reconcile.find_run(conn, "99999999")
    with pytest.raises(DomainError, match="matches 2 extraction runs"):
        reconcile.find_run(conn, "ABCDEF12")
    assert reconcile.find_run(conn, "abcdef12-0000-4000-8000-000000000001")["id"] \
        == runs[1]["id"]


def test_doctor_names_the_stuck_run_and_the_command_that_ends_it(conn, cfg):
    diff = _draft_run(conn, cfg)
    conn.execute("UPDATE extraction_runs SET started_at='2026-01-01T00:00:00Z' "
                 "WHERE uuid=?", (diff["extraction_run_uuid"],))
    check = next(c for c in run_checks(conn, cfg)["checks"]
                 if c["check"] == "stuck_drafts")
    prefix = diff["extraction_run_uuid"][:8]
    assert check["status"] == "warn"
    assert f"python -m lifecrm admin discard-run {prefix}" in check["detail"]

    reconcile.discard_run(conn, prefix)
    check = next(c for c in run_checks(conn, cfg)["checks"]
                 if c["check"] == "stuck_drafts")
    assert check["status"] == "ok"


# ---------------------------------------------------------------- 3.1 duplicates

def _dup_items(diff):
    return [i for i in diff["items"] if i["action"] == "possible_duplicate"]


def test_called_mom_and_called_tom_are_two_calls_not_a_duplicate(conn, cfg):
    s1 = _import(conn, cfg, "Called Mom after lunch.")
    reconcile.apply_diff(conn, _ingest(conn, cfg, _draft([s1], [_entry(
        "Called Mom", provenance={"char_start": 0, "char_end": 10})])))
    s2 = _import(conn, cfg, "Evening: called Tom about the trip.")
    diff = _ingest(conn, cfg, _draft([s2], [_entry(
        "Called Tom", provenance={"char_start": 9, "char_end": 19})]))
    assert _dup_items(diff) == []
    assert reconcile.apply_diff(conn, diff)["added"] == 1


def test_the_same_passage_corroborates_a_near_identical_title(conn, cfg):
    """A re-extraction of the same note that mis-hears Mom as Tom IS the same
    record: same source, overlapping passage."""
    s1 = _import(conn, cfg, "Called Mom after lunch.")
    reconcile.apply_diff(conn, _ingest(conn, cfg, _draft([s1], [_entry(
        "Called Mom", provenance={"char_start": 0, "char_end": 10})])))
    diff = _ingest(conn, cfg, _draft([s1], [_entry(
        "Called Tom", provenance={"char_start": 0, "char_end": 22})]))
    (dup,) = _dup_items(diff)
    assert dup["signals"] == ["similar_title", "same_passage"]
    assert dup["similarity"] == 0.9


def test_a_shared_entity_corroborates_a_near_identical_title(conn, cfg):
    jake = entities.create(conn, "person", "Jake")["id"]
    s1 = _import(conn, cfg, "Coffee with Jake at nine.")
    reconcile.apply_diff(conn, _ingest(conn, cfg, _draft([s1], [_entry(
        "Coffee with Jake", entities=[{"ref": "existing", "id": jake}])])))
    s2 = _import(conn, cfg, "Recap: coffee with Jack, really Jake.")
    diff = _ingest(conn, cfg, _draft([s2], [_entry(
        "Coffee with Jack", entities=[{"ref": "existing", "id": jake}])]))
    (dup,) = _dup_items(diff)
    assert dup["signals"] == ["similar_title", "shared_entity"]
    assert dup["of_entities"] == [{"id": jake, "name": "Jake", "type": "person",
                                   "role": "mention"}]


def test_a_related_title_needs_the_same_kind_and_category_and_a_signal(conn, cfg):
    body = "Easy pace, about 30 minutes around the water."
    s1 = _import(conn, cfg, "Ran 5k by the lake this morning.")
    reconcile.apply_diff(conn, _ingest(conn, cfg, _draft([s1], [_entry(
        "Ran 5k by the lake", cat="exercise", body=body)])))
    s2 = _import(conn, cfg, "Later note: ran 5k around the lake, easy pace.")

    same = _ingest(conn, cfg, _draft([s2], [_entry(
        "Ran 5k around the lake", cat="exercise", body=body + " Felt good.")]))
    (dup,) = _dup_items(same)
    assert dup["signals"] == ["similar_title", "similar_body"]

    other_kind = _ingest(conn, cfg, _draft([s2], [_entry(
        "Ran 5k around the lake", kind="plan", cat="exercise", body=body)]))
    assert _dup_items(other_kind) == []

    no_signal = _ingest(conn, cfg, _draft([s2], [_entry(
        "Ran 5k around the lake", cat="exercise")]))
    assert _dup_items(no_signal) == []


def test_an_exact_title_stands_alone_and_the_item_carries_the_incumbent(conn, cfg):
    _seed(conn, cfg)
    diff = _ingest(conn, cfg, golden_draft())
    dups = {d["of_title"]: d for d in _dup_items(diff)}
    assert len(dups) == 6
    boulder = dups["Bouldering at Movement with Jake"]
    assert boulder["signals"] == ["same_title"]
    assert boulder["of_body"] == "About 90 minutes; finally sent the blue V4."
    assert boulder["of_kind"] == "event"
    assert boulder["of_categories"] == {"primary": "exercise", "secondary": ["social"]}
    assert {e["name"] for e in boulder["of_entities"]} == {"Jake", "Movement",
                                                           "Bouldering"}
    # the extra fields are review aids only; apply still skips the duplicate
    assert reconcile.apply_diff(conn, diff)["skipped_duplicates"] == 6


# ---------------------------------------------------------------- doctor stays green

def _op_day_summary_set(conn, cfg, tmp_path):
    days.set_summary(conn, "2026-07-27", "Rewritten by hand.", actor="user")


def _op_summary_conflict_accepted(conn, cfg, tmp_path):
    draft = _draft([_golden_source(conn)], [], day={"summary": "A quieter day."})
    diff = _ingest(conn, cfg, draft)
    diff["day"]["summary"]["accepted"] = True
    reconcile.apply_diff(conn, diff)


def _op_discard_run(conn, cfg, tmp_path):
    reconcile.discard_run(conn, _ingest(conn, cfg, golden_draft())["extraction_run_uuid"])


def _op_entry_update_apply(conn, cfg, tmp_path):
    breakfast = _entry_row(conn, "Breakfast%")
    _close_via_entry_update(conn, cfg, breakfast["uuid"], _golden_source(conn),
                            {"char_start": 100, "char_end": 110})


def _op_transcribe_date(conn, cfg, tmp_path):
    from lifecrm import transcribe as tr
    from test_transcribe import FAKE_RESULT
    audio = tmp_path / "2026-07-20 21.30.00.m4a"
    audio.write_bytes(b"fake-audio-for-doctor")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tr, "transcribe_audio",
                   lambda cfg_, model, path, initial_prompt=None: dict(FAKE_RESULT))
        res = tr.process_file(conn, cfg, lambda: object(), audio,
                              date_override="2026-07-27")
    assert res["journal_date"] == "2026-07-27"


INTEGRITY_OPS = {
    "day summary set": _op_day_summary_set,
    "day summary conflict accepted": _op_summary_conflict_accepted,
    "admin discard-run": _op_discard_run,
    "entry_update apply": _op_entry_update_apply,
    "transcribe --date": _op_transcribe_date,
}


@pytest.mark.parametrize("op", list(INTEGRITY_OPS))
def test_doctor_stays_green_after(op, conn, cfg, tmp_path):
    _seed(conn, cfg)
    assert run_checks(conn, cfg)["failures"] == 0, "seed itself is not green"

    INTEGRITY_OPS[op](conn, cfg, tmp_path)
    conn.commit()

    report = run_checks(conn, cfg)
    assert report["failures"] == 0, (
        f"{op} broke: " + str([(c["check"], c.get("detail"))
                               for c in report["checks"] if c["status"] == "fail"]))

"""v2 behavior: multi-source days, cross-day closures, links, closable loops."""

import copy

import pytest
from conftest import golden_draft

from lifecrm import contract
from lifecrm.domain import corrections, entries, links, reconcile
from lifecrm.domain.util import DomainError
from lifecrm.importer import import_text
from test_reconcile import _ingest, _source_text


def _import(conn, cfg, text, date="2026-07-27"):
    return import_text(conn, cfg, text, journal_date=date)["source_id"]


def _entry(kind="event", cat="food", title="Lunch: burrito bowl", **kw):
    e = {"kind": kind, "categories": {"primary": cat}, "title": title}
    e.update(kw)
    return e


def _draft(source_ids, entries_list, date="2026-07-27", **top):
    d = {"contract_version": 2, "journal_date": date,
         "sources": [{"source_id": s} for s in source_ids],
         "entries": entries_list}
    d.update(top)
    return d


# ---------------------------------------------------------------- multi-source

def test_one_day_many_sources_one_draft(conn, cfg):
    s1 = _import(conn, cfg, "Morning standup ran long. Skipped breakfast.")
    s2 = _import(conn, cfg, "Evening recap. Lunch was a burrito bowl with Dana.")
    draft = _draft([s1, s2], [
        _entry(cat="work", title="Standup ran long", time_of_day="morning",
               provenance={"source_id": s1, "char_start": 0, "char_end": 26}),
        _entry(cat="food", title="Lunch: burrito bowl with Dana",
               entities=[{"ref": "new", "type": "person", "name": "Dana"}],
               provenance={"source_id": s2, "char_start": 15, "char_end": 50}),
    ])
    diff = _ingest(conn, cfg, draft)
    assert diff["source_ids"] == [s1, s2]
    summary = reconcile.apply_diff(conn, diff)
    assert summary["added"] == 2

    # one run, two sources in the join table
    rows = conn.execute("SELECT source_id FROM extraction_run_sources "
                        "ORDER BY source_id").fetchall()
    assert [r["source_id"] for r in rows] == [s1, s2]
    # per-entry provenance names WHICH source supports it
    prov = {r["title"]: r["source_id"] for r in conn.execute(
        "SELECT e.title, es.source_id FROM entry_sources es "
        "JOIN entries e ON e.id=es.entry_id")}
    assert prov["Standup ran long"] == s1
    assert prov["Lunch: burrito bowl with Dana"] == s2
    # both sources now count as processed
    unprocessed = conn.execute(
        "SELECT COUNT(*) c FROM sources s WHERE NOT EXISTS "
        "(SELECT 1 FROM extraction_run_sources rs JOIN extraction_runs r "
        "ON r.id=rs.extraction_run_id WHERE rs.source_id=s.id "
        "AND r.status='applied')").fetchone()["c"]
    assert unprocessed == 0


def test_multi_source_requires_per_entry_provenance(conn, cfg):
    s1 = _import(conn, cfg, "Note one.")
    s2 = _import(conn, cfg, "Note two.")
    draft = _draft([s1, s2], [_entry()])   # no provenance
    errs = contract.validate_draft(conn, draft,
                                   {s1: _source_text(conn, s1),
                                    s2: _source_text(conn, s2)})
    assert any("provenance: required in multi-source" in e for e in errs)


def test_provenance_source_must_be_declared(conn, cfg):
    s1 = _import(conn, cfg, "Note one.")
    s2 = _import(conn, cfg, "Note two.")
    draft = _draft([s1], [_entry(provenance={"source_id": s2,
                                             "char_start": 0, "char_end": 4})])
    errs = contract.validate_draft(conn, draft, {s1: _source_text(conn, s1)})
    assert any("not one of this draft's sources" in e for e in errs)


def test_cross_source_duplicate_detection(conn, cfg):
    s1 = _import(conn, cfg, "Quick note: burrito bowl for lunch.")
    diff = _ingest(conn, cfg, _draft([s1], [_entry(title="Lunch: burrito bowl")]))
    reconcile.apply_diff(conn, diff)

    s2 = _import(conn, cfg, "Evening: lunch was a burrito bowl, then gym.")
    diff2 = _ingest(conn, cfg, _draft([s2], [_entry(title="Lunch: burrito bowl")]))
    dups = [i for i in diff2["items"] if i["action"] == "possible_duplicate"]
    assert len(dups) == 1, "cross-source duplicates must be caught (same day)"


def test_retracts_stay_scoped_to_overlapping_sources(conn, cfg):
    s1 = _import(conn, cfg, "Note about the workout.")
    diff = _ingest(conn, cfg, _draft(
        [s1], [_entry(cat="exercise", title="Evening workout")]))
    reconcile.apply_diff(conn, diff)

    # A different source, same day, unrelated content: no retract proposals.
    s2 = _import(conn, cfg, "Totally separate quick note.")
    diff2 = _ingest(conn, cfg, _draft([s2], [_entry(cat="idea", kind="idea",
                                                    title="A new idea")]))
    assert all(i["action"] != "retract" for i in diff2["items"])
    assert any(i["action"] == "unchanged_confirmed" for i in diff2["items"])


# ---------------------------------------------------------------- closable loops

def test_close_and_reopen(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="task",
                       title="Renew passport", primary_category="chores",
                       due_at="2026-08-15")
    corrections.close_entry(conn, e["uuid"], "done")
    row = entries.get_by_uuid(conn, e["uuid"])
    assert row["closure"] == "done" and row["closed_at"] is not None
    assert row["status"] == "active"   # closing is not retracting

    with pytest.raises(DomainError, match="already closed"):
        corrections.close_entry(conn, e["uuid"], "dropped")

    corrections.reopen_entry(conn, e["uuid"])
    row = entries.get_by_uuid(conn, e["uuid"])
    assert row["closure"] is None and row["closed_at"] is None
    kinds = [r["change_kind"] for r in conn.execute(
        "SELECT change_kind FROM entry_revisions WHERE entry_uuid=? ORDER BY id",
        (e["uuid"],))]
    assert kinds == ["create", "close", "reopen"]


def test_close_rejects_bad_input(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="task",
                       title="X", primary_category="chores")
    with pytest.raises(DomainError, match="closure"):
        corrections.close_entry(conn, e["uuid"], "finished")
    corrections.retract_entry(conn, e["uuid"])
    with pytest.raises(DomainError, match="only active"):
        corrections.close_entry(conn, e["uuid"], "done")
    with pytest.raises(DomainError, match="not closed"):
        corrections.reopen_entry(conn, e["uuid"])


def test_dropped_stays_distinct_from_retracted(conn):
    """Retracting = the record was wrong. Dropping = it happened and I chose
    not to do it. Distinguishable in 2040."""
    a = entries.create(conn, journal_date="2026-07-27", kind="plan",
                       title="Plan A", primary_category="other")
    b = entries.create(conn, journal_date="2026-07-27", kind="plan",
                       title="Plan B", primary_category="other")
    corrections.close_entry(conn, a["uuid"], "dropped")
    corrections.retract_entry(conn, b["uuid"], reason="never actually planned")
    ra = entries.get_by_uuid(conn, a["uuid"])
    rb = entries.get_by_uuid(conn, b["uuid"])
    assert (ra["status"], ra["closure"]) == ("active", "dropped")
    assert (rb["status"], rb["closure"]) == ("retracted", None)


# ---------------------------------------------------------------- entry_updates

def _apply_task_day(conn, cfg):
    s1 = _import(conn, cfg, "I need to renew my passport before the trip.",
                 date="2026-07-21")
    draft = _draft([s1], [_entry(kind="task", cat="chores",
                                 title="Renew passport", due_at="2026-08-15")],
                   date="2026-07-21")
    diff = _ingest(conn, cfg, draft)
    reconcile.apply_diff(conn, diff)
    return conn.execute("SELECT uuid FROM entries WHERE title='Renew passport'"
                        ).fetchone()["uuid"]


def test_entry_update_target_changing_makes_the_diff_stale(conn, cfg):
    """A cross-day item shows the target's CURRENT state for review. If the
    target moves underneath, that review is of a state that no longer exists —
    even though the diff's own day is untouched."""
    task_uuid = _apply_task_day(conn, cfg)
    s2 = _import(conn, cfg, "Finally renewed the passport this morning.",
                 date="2026-07-27")
    draft = _draft([s2], [], date="2026-07-27", entry_updates=[{
        "target_uuid": task_uuid,
        "set": {"closure": "done", "closed_at": "2026-07-27T15:00:00Z"},
        "provenance": {"char_start": 0, "char_end": 41}}])
    diff = _ingest(conn, cfg, draft)
    for i in diff["items"]:
        if i["action"] == "entry_update":
            i["accepted"] = True

    corrections.close_entry(conn, task_uuid, "dropped")   # closed by hand meanwhile

    with pytest.raises(DomainError, match="re-run ingest"):
        reconcile.apply_diff(conn, diff)
    assert entries.get_by_uuid(conn, task_uuid)["closure"] == "dropped"


def test_entry_update_closes_across_days(conn, cfg):
    task_uuid = _apply_task_day(conn, cfg)

    text = "Finally renewed the passport this morning. Felt great."
    s2 = _import(conn, cfg, text, date="2026-07-27")
    draft = _draft([s2], [], date="2026-07-27", entry_updates=[{
        "target_uuid": task_uuid,
        "set": {"closure": "done", "closed_at": "2026-07-27T15:00:00Z"},
        "provenance": {"char_start": 0, "char_end": 45},
        "note": "closed by morning note",
    }])
    diff = _ingest(conn, cfg, draft)
    item = next(i for i in diff["items"] if i["action"] == "entry_update")
    assert item["requires_decision"] is True and item["accepted"] is None
    assert item["target_date"] == "2026-07-21"   # reconciles ACROSS days

    # default apply: never auto-applied
    summary = reconcile.apply_diff(conn, diff)
    assert summary["closed"] == 0 and summary["skipped_decisions"] >= 1
    assert entries.get_by_uuid(conn, task_uuid)["closure"] is None

    # accepted: closes, with provenance tracing to the closing sentence
    diff2 = _ingest(conn, cfg, copy.deepcopy(
        _draft([s2], [], date="2026-07-27", entry_updates=[{
            "target_uuid": task_uuid,
            "set": {"closure": "done", "closed_at": "2026-07-27T15:00:00Z"},
            "provenance": {"char_start": 0, "char_end": 45}}])))
    for i in diff2["items"]:
        if i["action"] == "entry_update":
            i["accepted"] = True
    summary = reconcile.apply_diff(conn, diff2)
    assert summary["closed"] == 1
    row = entries.get_by_uuid(conn, task_uuid)
    assert row["closure"] == "done"
    assert row["closed_at"] == "2026-07-27T15:00:00Z"
    excerpt_row = conn.execute(
        "SELECT es.char_start, es.char_end, t.text FROM entry_sources es "
        "JOIN transcripts t ON t.id=es.transcript_id "
        "WHERE es.entry_id=(SELECT id FROM entries WHERE uuid=?) "
        "AND es.source_id=?", (task_uuid, s2)).fetchone()
    assert excerpt_row is not None
    excerpt = excerpt_row["text"][excerpt_row["char_start"]:excerpt_row["char_end"]]
    assert "renewed the passport" in excerpt


def test_entry_update_validation(conn, cfg):
    task_uuid = _apply_task_day(conn, cfg)
    s2 = _import(conn, cfg, "Some note.", date="2026-07-27")
    base = {"target_uuid": task_uuid,
            "set": {"closure": "done"},
            "provenance": {"char_start": 0, "char_end": 4}}

    def errs(mutate):
        upd = copy.deepcopy(base)
        mutate(upd)
        draft = _draft([s2], [], date="2026-07-27", entry_updates=[upd])
        return contract.validate_draft(conn, draft, {s2: _source_text(conn, s2)})

    assert any("not settable" in e for e in errs(
        lambda u: u["set"].update(title="New title")))
    assert any("closure" in e for e in errs(
        lambda u: u["set"].update(closure="finished")))
    assert any("provenance: required" in e for e in errs(
        lambda u: u.pop("provenance")))
    assert any("does not exist" in e for e in errs(
        lambda u: u.update(target_uuid="00000000-0000-0000-0000-000000000000")))


# ---------------------------------------------------------------- links

def test_links_from_draft_indexes_and_uuids(conn, cfg):
    task_uuid = _apply_task_day(conn, cfg)
    s2 = _import(conn, cfg, "Renewed the passport, then planned the trip legs.",
                 date="2026-07-27")
    draft = _draft([s2], [
        _entry(kind="event", cat="chores", title="Renewed passport at the office",
               provenance={"char_start": 0, "char_end": 21}),
        _entry(kind="plan", cat="travel", title="Plan trip legs",
               provenance={"char_start": 23, "char_end": 48}),
    ], date="2026-07-27", links=[
        {"from_index": 0, "to_uuid": task_uuid, "type": "resolves"},
        {"from_index": 1, "to_index": 0, "type": "relates"},
    ])
    diff = _ingest(conn, cfg, draft)
    assert diff["counts"]["links"] == 2
    summary = reconcile.apply_diff(conn, diff)
    assert summary["links_created"] == 2

    resolved = conn.execute(
        "SELECT l.from_uuid, l.to_uuid, l.type, l.actor FROM entry_links l "
        "WHERE l.type='resolves'").fetchone()
    assert resolved["to_uuid"] == task_uuid
    assert resolved["actor"] == "extraction"
    from_entry = entries.get_by_uuid(conn, resolved["from_uuid"])
    assert from_entry["title"] == "Renewed passport at the office"


def test_link_to_skipped_duplicate_resolves_to_survivor(conn, cfg):
    s1 = _import(conn, cfg, "Went bouldering tonight.")
    diff = _ingest(conn, cfg, _draft(
        [s1], [_entry(cat="exercise", title="Bouldering session")]))
    reconcile.apply_diff(conn, diff)
    survivor = conn.execute("SELECT uuid FROM entries").fetchone()["uuid"]

    s2 = _import(conn, cfg, "More on bouldering: felt strong, new idea for training.")
    draft = _draft([s2], [
        _entry(cat="exercise", title="Bouldering session"),     # dup -> skipped
        _entry(kind="idea", cat="idea", title="Training cycle idea"),
    ], links=[{"from_index": 1, "to_index": 0, "type": "relates"}])
    diff2 = _ingest(conn, cfg, draft)
    summary = reconcile.apply_diff(conn, diff2)
    assert summary["skipped_duplicates"] == 1
    assert summary["links_created"] == 1
    link = conn.execute("SELECT * FROM entry_links").fetchone()
    assert link["to_uuid"] == survivor   # resolved to the surviving entry


def test_link_domain_validation(conn):
    a = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="A", primary_category="other")
    b = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="B", primary_category="other")
    with pytest.raises(DomainError, match="unknown link type"):
        links.add(conn, a["uuid"], b["uuid"], "follows")
    with pytest.raises(DomainError, match="itself"):
        links.add(conn, a["uuid"], a["uuid"], "relates")
    with pytest.raises(DomainError, match="does not exist"):
        links.add(conn, a["uuid"], "00000000-0000-0000-0000-000000000000", "relates")
    assert links.add(conn, a["uuid"], b["uuid"], "continues") is True
    assert links.add(conn, a["uuid"], b["uuid"], "continues") is False  # idempotent
    result = links.for_entry(conn, a["uuid"])
    assert result["outgoing"][0]["other_title"] == "B"
    result_b = links.for_entry(conn, b["uuid"])
    assert result_b["incoming"][0]["other_title"] == "A"
    links.remove(conn, a["uuid"], b["uuid"], "continues")
    with pytest.raises(DomainError, match="no continues link"):
        links.remove(conn, a["uuid"], b["uuid"], "continues")


def test_purge_cascades_links_but_keeps_revisions(conn):
    """The deliberate asymmetry: links to purged entries die with them;
    revision history survives the purge."""
    a = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="A", primary_category="other")
    b = entries.create(conn, journal_date="2026-07-28", kind="event",
                       title="B", primary_category="other")
    links.add(conn, b["uuid"], a["uuid"], "continues")
    corrections.purge_day(conn, "2026-07-27")
    assert conn.execute("SELECT COUNT(*) c FROM entry_links").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM entry_revisions WHERE entry_uuid=?",
                        (a["uuid"],)).fetchone()["c"] >= 1


def test_contract_link_validation(conn, cfg):
    s = _import(conn, cfg, "A note.")
    draft = _draft([s], [_entry()], links=[
        {"from_index": 0, "to_index": 0, "type": "relates"}])
    errs = contract.validate_draft(conn, draft, {s: _source_text(conn, s)})
    assert any("itself" in e for e in errs)

    draft = _draft([s], [_entry()], links=[
        {"from_index": 5, "to_index": 0, "type": "relates"}])
    errs = contract.validate_draft(conn, draft, {s: _source_text(conn, s)})
    assert any("valid index" in e for e in errs)

    draft = _draft([s], [_entry()], links=[
        {"from_index": 0, "to_uuid": "nope", "type": "sequel"}])
    errs = contract.validate_draft(conn, draft, {s: _source_text(conn, s)})
    assert any(".type" in e for e in errs)
    assert any("does not exist" in e for e in errs)
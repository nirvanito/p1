import copy

import pytest
from conftest import golden_draft

from lifecrm import contract
from lifecrm.domain import corrections, entries, reconcile
from lifecrm.domain.sources import content_hash_text
from lifecrm.domain.util import DomainError
from lifecrm.importer import import_text


def _source_text(conn, source_id):
    return conn.execute("SELECT text FROM transcripts WHERE source_id=? "
                        "ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()["text"]


def _resolve_refs(conn, draft):
    """Convert 'new' entity refs to 'existing' where an active entity already
    matches — exactly what the /log skill does via `entities match` before
    submitting a draft (the contract rejects unresolved exact matches)."""
    for entry in draft.get("entries", []):
        resolved = []
        for ref in entry.get("entities", []) or []:
            if ref.get("ref") == "new":
                hit = conn.execute(
                    "SELECT id FROM entities WHERE status='active' AND type=? "
                    "AND name=? COLLATE NOCASE", (ref["type"], ref["name"])).fetchone()
                if hit:
                    ref = {"ref": "existing", "id": hit["id"],
                           "role": ref.get("role", "mention")}
            resolved.append(ref)
        if resolved:
            entry["entities"] = resolved
    return draft


def _ingest(conn, cfg, draft):
    """Validate + materialize + reconcile, exactly like the ingest command."""
    _resolve_refs(conn, draft)
    texts, inline = {}, None
    for s in draft["sources"]:
        if "source_id" in s:
            texts[s["source_id"]] = _source_text(conn, s["source_id"])
        else:
            inline = s["text"]
    errors = contract.validate_draft(conn, draft, texts, inline)
    assert errors == [], errors
    for i, s in enumerate(draft["sources"]):
        if "text" in s:
            imported = import_text(conn, cfg, s["text"],
                                   journal_date=draft["journal_date"],
                                   source_type=s.get("type", "pasted"))
            draft["sources"][i] = {"source_id": imported["source_id"]}
    source_ids = [s["source_id"] for s in draft["sources"]]
    all_texts = [_source_text(conn, sid) for sid in sorted(source_ids)]
    run = reconcile.create_extraction_run(
        conn, source_ids=source_ids, provider="test",
        model=None, interface="manual", contract_version=2,
        input_hash=content_hash_text("\n".join(all_texts)))
    return reconcile.build_diff(conn, draft, run)


def test_empty_day_collapses_to_all_additions(conn, cfg):
    diff = _ingest(conn, cfg, golden_draft())
    assert diff["counts"]["add"] == 6
    assert diff["counts"]["possible_duplicate"] == 0
    assert diff["counts"]["requires_decision"] == 0


def test_apply_creates_everything(conn, cfg):
    diff = _ingest(conn, cfg, golden_draft())
    summary = reconcile.apply_diff(conn, diff)
    assert summary["added"] == 6
    assert summary["entities_created"] == 4       # Dana, Jake, Movement, Bouldering
    assert summary["day_measurements"] == 3
    day = conn.execute("SELECT summary FROM days WHERE journal_date='2026-07-27'").fetchone()
    assert "quarterly report" in day["summary"]
    # every entry has provenance back to the source
    n = conn.execute("SELECT COUNT(*) c FROM entry_sources").fetchone()["c"]
    assert n == 6
    # applying the reviewed diff confirms entries
    unconfirmed = conn.execute(
        "SELECT COUNT(*) c FROM entries WHERE confirmed_at IS NULL").fetchone()["c"]
    assert unconfirmed == 0


def test_reingest_same_draft_is_stable(conn, cfg):
    draft = golden_draft()
    diff = _ingest(conn, cfg, draft)
    reconcile.apply_diff(conn, diff)
    uuids_before = {r["uuid"] for r in conn.execute("SELECT uuid FROM entries")}

    diff2 = _ingest(conn, cfg, golden_draft())
    # identical titles -> possible duplicates, not blind additions
    assert diff2["counts"]["add"] == 0
    assert diff2["counts"]["possible_duplicate"] == 6
    summary = reconcile.apply_diff(conn, diff2)
    assert summary["added"] == 0
    assert summary["skipped_duplicates"] == 6
    uuids_after = {r["uuid"] for r in conn.execute("SELECT uuid FROM entries")}
    assert uuids_before == uuids_after     # stable IDs across reconciliation


def test_confirmed_records_never_silently_overwritten(conn, cfg):
    diff = _ingest(conn, cfg, golden_draft())
    reconcile.apply_diff(conn, diff)
    target = conn.execute(
        "SELECT uuid FROM entries WHERE title LIKE 'Lunch%'").fetchone()["uuid"]
    # user manually corrects the entry
    corrections.edit_entry(conn, target, {"title": "Lunch: carnitas burrito bowl"})

    # a re-extraction proposes a different title for the same entry
    draft = golden_draft()
    draft["entries"] = [e for e in draft["entries"] if "Lunch" in e["title"]]
    draft["entries"][0]["uuid"] = target
    draft["entries"][0]["title"] = "Lunch: some burrito"
    draft["day"] = {}
    diff2 = _ingest(conn, cfg, draft)
    upd = [i for i in diff2["items"] if i["action"] == "update"]
    assert len(upd) == 1
    assert upd[0]["requires_decision"] is True
    assert upd[0]["accepted"] is None

    summary = reconcile.apply_diff(conn, diff2)
    assert summary["skipped_decisions"] >= 1
    row = conn.execute("SELECT title FROM entries WHERE uuid=?", (target,)).fetchone()
    assert row["title"] == "Lunch: carnitas burrito bowl"   # untouched

    # explicit acceptance applies it as a user decision
    diff3 = _ingest(conn, cfg, copy.deepcopy(draft))
    for item in diff3["items"]:
        if item["action"] == "update":
            item["accepted"] = True
    reconcile.apply_diff(conn, diff3)
    row = conn.execute("SELECT title FROM entries WHERE uuid=?", (target,)).fetchone()
    assert row["title"] == "Lunch: some burrito"


def test_retraction_only_proposed_never_automatic(conn, cfg):
    draft = golden_draft()
    diff = _ingest(conn, cfg, draft)
    reconcile.apply_diff(conn, diff)
    source_id = diff["source_ids"][0]

    # re-extraction of the SAME source with one entry omitted
    draft2 = golden_draft()
    omitted = draft2["entries"].pop()   # drop the idea entry
    draft2["sources"] = [{"source_id": source_id}]
    draft2["day"] = {}
    diff2 = _ingest(conn, cfg, draft2)
    props = [i for i in diff2["items"] if i["action"] == "retract"]
    assert len(props) == 1
    assert omitted["title"] == props[0]["title"]

    # default apply does NOT retract
    reconcile.apply_diff(conn, diff2)
    status = conn.execute("SELECT status FROM entries WHERE title=?",
                          (omitted["title"],)).fetchone()["status"]
    assert status == "active"

    # accepted retraction does
    diff3 = _ingest(conn, cfg, copy.deepcopy(draft2))
    for item in diff3["items"]:
        if item["action"] == "retract":
            item["accepted"] = True
    reconcile.apply_diff(conn, diff3)
    status = conn.execute("SELECT status FROM entries WHERE title=?",
                          (omitted["title"],)).fetchone()["status"]
    assert status == "retracted"


def test_provenance_round_trip(conn, cfg):
    draft = golden_draft()
    text = draft["sources"][0]["text"]
    diff = _ingest(conn, cfg, draft)
    reconcile.apply_diff(conn, diff)
    # char-offset provenance
    row = conn.execute(
        "SELECT es.char_start, es.char_end, t.text FROM entry_sources es "
        "JOIN entries e ON e.id=es.entry_id "
        "JOIN transcripts t ON t.id=es.transcript_id "
        "WHERE e.title LIKE 'Breakfast%'").fetchone()
    assert row["char_start"] == 0
    excerpt = row["text"][row["char_start"]:row["char_end"]]
    assert excerpt in text
    # segment provenance
    row = conn.execute(
        "SELECT es.segment_start, es.segment_end FROM entry_sources es "
        "JOIN entries e ON e.id=es.entry_id "
        "WHERE e.title LIKE 'Bouldering%'").fetchone()
    assert (row["segment_start"], row["segment_end"]) == (4, 5)
    # original extraction JSON preserved
    orig = conn.execute(
        "SELECT es.original_json FROM entry_sources es JOIN entries e "
        "ON e.id=es.entry_id WHERE e.title LIKE 'Bouldering%'").fetchone()["original_json"]
    assert "blue V4" in orig


def test_day_measurement_conflict_requires_decision(conn, cfg):
    diff = _ingest(conn, cfg, golden_draft())
    reconcile.apply_diff(conn, diff)

    draft = golden_draft()
    draft["entries"] = []
    draft["day"]["measurements"] = [{"metric": "mood", "numeric_value": 4}]
    del draft["day"]["summary"]
    diff2 = _ingest(conn, cfg, draft)
    md = diff2["day"]["measurements"][0]
    assert md["action"] == "conflict"

    reconcile.apply_diff(conn, diff2)   # not accepted -> unchanged
    val = conn.execute("SELECT numeric_value FROM measurements WHERE metric='mood' "
                       "AND status='active'").fetchone()["numeric_value"]
    assert val == 7

    diff3 = _ingest(conn, cfg, copy.deepcopy(draft))
    diff3["day"]["measurements"][0]["accepted"] = True
    reconcile.apply_diff(conn, diff3)
    rows = conn.execute("SELECT numeric_value, status FROM measurements "
                        "WHERE metric='mood' ORDER BY id").fetchall()
    assert [(r["numeric_value"], r["status"]) for r in rows] == [
        (7, "retracted"), (4, "active")]


def test_apply_twice_rejected(conn, cfg):
    diff = _ingest(conn, cfg, golden_draft())
    reconcile.apply_diff(conn, diff)
    with pytest.raises(DomainError, match="already applied"):
        reconcile.apply_diff(conn, diff)


# --- staleness ---------------------------------------------------------------
# `apply_twice_rejected` above only guards the same diff twice. Two SEPARATE
# ingests of one day produce two extraction runs, so both diffs looked
# applicable — and the second one was built against the day BEFORE the first
# was applied, so applying it added every entry a second time. 6 -> 12.

def test_second_diff_of_the_same_day_is_refused_as_stale(conn, cfg):
    diff1 = _ingest(conn, cfg, golden_draft())
    diff2 = _ingest(conn, cfg, golden_draft())      # built against the same empty day
    reconcile.apply_diff(conn, diff1)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 6

    with pytest.raises(DomainError, match="re-run ingest"):
        reconcile.apply_diff(conn, diff2)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 6


def test_reingest_after_a_stale_refusal_still_works(conn, cfg):
    """The escape hatch has to be the documented one: ingest again."""
    diff1 = _ingest(conn, cfg, golden_draft())
    diff2 = _ingest(conn, cfg, golden_draft())
    reconcile.apply_diff(conn, diff1)
    with pytest.raises(DomainError):
        reconcile.apply_diff(conn, diff2)

    fresh = _ingest(conn, cfg, golden_draft())
    summary = reconcile.apply_diff(conn, fresh)
    assert summary["added"] == 0                    # every entry is a known duplicate
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 6


def test_an_edit_underneath_makes_the_diff_stale(conn, cfg):
    """Not just double-apply: any change to the day invalidates the review."""
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    draft = golden_draft()
    draft["entries"].append(_new_entry_titled("Evening walk"))
    diff = _ingest(conn, cfg, draft)

    victim = conn.execute("SELECT uuid FROM entries WHERE title LIKE 'Lunch%'"
                          ).fetchone()["uuid"]
    corrections.edit_entry(conn, victim, {"title": "Lunch: chicken burrito bowl, no rice"})

    with pytest.raises(DomainError, match="re-run ingest"):
        reconcile.apply_diff(conn, diff)


def test_an_unrelated_day_does_not_make_the_diff_stale(conn, cfg):
    """The guard is per-day: work on another day must not invalidate a diff."""
    diff = _ingest(conn, cfg, golden_draft())
    other = import_text(conn, cfg, "Different day entirely.",
                        journal_date="2026-07-26", source_type="pasted")
    other_draft = {"contract_version": 2, "journal_date": "2026-07-26",
                   "sources": [{"source_id": other["source_id"]}],
                   "entries": [_new_entry_titled("Something else")]}
    reconcile.apply_diff(conn, _ingest(conn, cfg, other_draft))

    summary = reconcile.apply_diff(conn, diff)      # still valid
    assert summary["added"] == 6


def _new_entry_titled(title):
    return {"kind": "event", "categories": {"primary": "other"}, "title": title}


def test_new_entity_reused_within_apply(conn, cfg):
    draft = golden_draft()
    draft["entries"].append({
        "kind": "event", "categories": {"primary": "social"},
        "title": "Coffee with Jake",
        "entities": [{"ref": "new", "type": "person", "name": "Jake"}]})
    diff = _ingest(conn, cfg, draft)
    summary = reconcile.apply_diff(conn, diff)
    # 'Jake' appears in two entries but is created once
    n = conn.execute("SELECT COUNT(*) c FROM entities WHERE name='Jake'").fetchone()["c"]
    assert n == 1
    assert summary["entities_created"] == 4

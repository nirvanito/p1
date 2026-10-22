"""Demo samples for the notes connector: a Google Keep Takeout (Samsung) and
a Mac's NoteStore.sqlite (iPhone), both rendered from `persona.NOTES`.

What matters is that the story survives the trip through a real export
format and the real importer: the Bled packing list lands on the first day of
the trip four years back, the gift list carries the voucher Alex gave, the
packing list is shared with Jake — and the trash, and on the Mac a locked
note, are counted but never kept. Then the usual contract: re-applying is a
no-op and doctor stays green, on a fresh database and on a seeded one.
"""

import json
from datetime import date

import pytest

from lifecrm.connectors import notes
from lifecrm.demo import persona
from lifecrm.demo.sources import notes as sample
from lifecrm.domain import observations as obs
from lifecrm.doctor import run_checks
from test_doctor import _seed

ANCHOR = date(2026, 10, 21)


def _by_title(scan):
    return {r["title"]: r for r in scan["records"]}


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return result


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_every_persona_note_comes_through(tmp_path, cfg, combo):
    scan = notes.scan(sample.write_sample(tmp_path, ANCHOR, combo), cfg)
    assert set(_by_title(scan)) == {n.title for n in persona.NOTES}


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_bled_packing_list_is_dated_on_the_trip_four_years_back(tmp_path, cfg, combo):
    rec = _by_title(notes.scan(sample.write_sample(tmp_path, ANCHOR, combo), cfg))[
        "Bled packing list"]
    first_day = persona.bled_trip_days(ANCHOR)[0]
    assert rec["journal_date"] == first_day.isoformat()
    assert first_day.year == ANCHOR.year - 4
    assert "euro cash for the pletna boat" in rec["body"]


def test_android_is_a_keep_takeout_with_checklists_labels_and_sharing(tmp_path, cfg):
    z = sample.write_sample(tmp_path, ANCHOR, "android")
    assert z.name == "takeout-20261021T120000Z-001.zip"
    scan = notes.scan(z, cfg)
    assert scan["source_kind"] == "keep"
    recs = _by_title(scan)
    assert "- [x] ceramics class voucher (Austin Clay Studio)" in recs["Gift ideas for Maya"]["body"]
    assert "- [ ] the Patagonia jacket in sage" in recs["Gift ideas for Maya"]["body"]
    bled = json.loads(recs["Bled packing list"]["value_json"])
    assert bled["archived"] is True and bled["labels"] == ["travel"]
    jake = persona.person("jake").email
    assert recs["Bled packing list"]["participants"] == [
        {"kind": "email", "value": jake, "role": "participant"}]
    assert {"kind": "email", "value": jake, "display_name": None} in scan["handles"]
    assert scan["account"] == persona.OWNER.email
    assert "1 × note in Keep's trash (deleted — not kept)" in scan["skipped"]


def test_iphone_is_a_notestore_with_folders_and_a_locked_note(tmp_path, cfg):
    store = sample.write_sample(tmp_path, ANCHOR, "iphone")
    assert store.name == "NoteStore.sqlite"
    scan = notes.scan(store, cfg)
    assert scan["source_kind"] == "apple_notes"
    recs = _by_title(scan)
    assert json.loads(recs["Bled packing list"]["value_json"])["folder"] == "Travel"
    assert json.loads(recs["Board deck outline"]["value_json"])["attachments"] == 1
    assert recs["Board deck outline"]["body"].startswith(
        "Board deck outline\n1. Customer story: the warehouse pilot")
    assert scan["account"].startswith("apple_notes:")
    joined = " | ".join(scan["skipped"])
    assert "locked note" in joined and "Recently Deleted" in joined


def test_the_gift_list_was_last_edited_the_night_of_mayas_birthday(tmp_path, cfg):
    rec = _by_title(notes.scan(sample.write_sample(tmp_path, ANCHOR, "android"), cfg))[
        "Gift ideas for Maya"]
    created_day = persona.day(ANCHOR, -25).isoformat()
    birthday = persona.birthday_date(persona.person("maya"), ANCHOR)
    assert rec["journal_date"] == created_day
    assert rec["last_modified_at"] == persona.utc_iso(
        persona.local_dt(ANCHOR, (birthday - ANCHOR).days, "21:30"))


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_a_sample_is_deterministic_for_its_anchor(tmp_path, cfg, combo):
    a = sample.write_sample(tmp_path / "a", ANCHOR, combo)
    b = sample.write_sample(tmp_path / "b", ANCHOR, combo)
    assert a.read_bytes() == b.read_bytes()
    assert notes.scan(a, cfg)["records"] == notes.scan(b, cfg)["records"]
    # rewriting into the same folder replaces the sample, it does not add to it
    again = sample.write_sample(tmp_path / "a", ANCHOR, combo)
    assert again.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_moves_with_the_anchor(tmp_path, cfg, combo):
    later = date(2027, 3, 1)
    rec = _by_title(notes.scan(sample.write_sample(tmp_path, later, combo), cfg))["Book list"]
    assert rec["journal_date"] == persona.day(later, -16).isoformat()


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_a_sample_applies_once_and_reapplies_as_identical(tmp_path, cfg, conn, combo):
    target = sample.write_sample(tmp_path, ANCHOR, combo)
    first = _apply(conn, notes.scan(target, cfg))
    assert first["added"] == len(persona.NOTES)
    again = _apply(conn, notes.scan(target, cfg))
    assert again["added"] == 0 and again["identical"] == len(persona.NOTES)
    assert "removed" not in again
    for table in ("entries", "measurements", "mentions", "entity_aliases"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0
    assert run_checks(conn, cfg)["failures"] == 0


def test_both_samples_into_one_database_stay_apart(tmp_path, cfg, conn):
    """Keep and Apple Notes are different accounts: importing one never
    removes the other's notes."""
    _apply(conn, notes.scan(sample.write_sample(tmp_path / "k", ANCHOR, "android"), cfg))
    result = _apply(conn, notes.scan(sample.write_sample(tmp_path / "m", ANCHOR, "iphone"), cfg))
    assert result["added"] == len(persona.NOTES) and "removed" not in result
    n = conn.execute("SELECT COUNT(*) c FROM observations WHERE stream='note' "
                     "AND status='active'").fetchone()["c"]
    assert n == 2 * len(persona.NOTES)


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_doctor_stays_green_after_a_notes_import_on_a_logged_day(tmp_path, cfg, conn, combo):
    _seed(conn, cfg)
    assert run_checks(conn, cfg)["failures"] == 0, "seed itself is not green"
    _apply(conn, notes.scan(sample.write_sample(tmp_path, ANCHOR, combo), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [(c["check"], c.get("detail"))
                                     for c in report["checks"] if c["status"] == "fail"]


def test_the_generator_and_the_importer_agree_on_the_protobuf(tmp_path):
    text = "Title\nline \N{OBJECT REPLACEMENT CHARACTER} with an image\nend"
    assert notes.note_text(sample.note_data(text)) == text


def test_a_combo_without_notes_is_refused():
    with pytest.raises(ValueError, match="no sample for combo 'blackberry'"):
        sample.write_sample(None, ANCHOR, "blackberry")

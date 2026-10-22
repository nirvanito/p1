"""Obsidian mirror: a read-only vault rendered from the database.

The vault is seeded the way real data arrives — a reviewed journal day (the
golden draft) plus a second day with open loops, a Health Connect import, an
AI chat export and a feed of texts, a calendar event and a note through the
shared observed-tier write layer — and every export runs on a read-only
connection, which is the proof that the mirror cannot write back.

What matters most is what the exporter must never do: touch a file it did not
generate, leave a stale generated note behind, or render the same database
two different ways.
"""

import json
import os
import zipfile

import pytest
from conftest import FIXTURES, cli_project, golden_draft
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import health_connect as hc
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.connectors import ai_chat, base
from lifecrm.doctor import run_checks
from lifecrm.domain import corrections, entities, links
from lifecrm.domain import observations as obs
from lifecrm.domain import reconcile
from lifecrm.domain.util import DomainError
from lifecrm.export import (NOTICE, VAULT_MANIFEST, _amount, build_vault,
                            export_obsidian, is_generated, safe_stem, unique_stems)
from test_health_connect import make_hc_db
from test_reconcile import _ingest

runner = CliRunner()

JAKE = "+15125550142"
ME = "+15125550100"
ELLIS = "frontdesk@ellisdental.example"

DAY2_TEXT = ("Need to book a dentist cleaning before August 5th. "
             "Jake wants to climb again next week.")


# ---- seed

def _feed_scan(records, connector="test_feed"):
    """Texts, a calendar event and a note, shaped exactly as connectors emit
    them: handles, never names."""
    return base.empty_scan(
        connector, account=ME, source_file="feed.json", records=records,
        handles=[{"kind": "phone", "value": JAKE, "display_name": "Jake M"},
                 {"kind": "phone", "value": ME, "display_name": "you (this phone)"},
                 {"kind": "email", "value": ELLIS, "display_name": "Ellis Dental"}])


def _text(ext_id, at, direction, body, jd="2026-07-28"):
    them, me = ({"kind": "phone", "value": JAKE},
                {"kind": "phone", "value": ME})
    sender, recipient = (them, me) if direction == "in" else (me, them)
    return {"stream": "imessage", "external_id": ext_id, "journal_date": jd,
            "started_at": at, "direction": direction, "body": body,
            "origin_app": "messages", "last_modified_at": at,
            "participants": [{**sender, "role": "sender"},
                             {**recipient, "role": "recipient"}]}


FEED = [
    _text("T1", "2026-07-28T23:10:00Z", "in", "same wall next week?"),
    _text("T2", "2026-07-28T23:12:00Z", "out", "yes! Veracruz after?"),
    {"stream": "calendar_event", "external_id": "C1", "journal_date": "2026-07-28",
     "started_at": "2026-07-28T14:00:00Z", "ended_at": "2026-07-28T15:00:00Z",
     "title": "Dentist — Dr. Ellis", "origin_app": "calendar",
     "participants": [{"kind": "email", "value": ELLIS, "role": "organizer"}]},
    {"stream": "note", "external_id": "N1", "journal_date": "2026-07-28",
     "started_at": "2026-07-28T16:00:00Z", "title": "Lake Bled packing list",
     "body": "rain jacket, hiking boots", "origin_app": "notes"},
]


def _apply_scan(conn, scan):
    return obs.apply_plan(conn, scan, obs.build_plan(conn, scan))


def _entity_id(conn, name):
    return conn.execute("SELECT id FROM entities WHERE name=? AND status='active'",
                        (name,)).fetchone()["id"]


def _seed(conn, cfg, tmp_path):
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    idea = conn.execute("SELECT uuid FROM entries WHERE title LIKE 'App to%'"
                        ).fetchone()["uuid"]
    corrections.close_entry(conn, idea, "obsolete", note="superseded by this CRM")
    boulder = conn.execute("SELECT uuid FROM entries WHERE title LIKE 'Bouldering%'"
                           ).fetchone()["uuid"]
    links.add(conn, idea, boulder, "relates")

    reconcile.apply_diff(conn, _ingest(conn, cfg, {
        "contract_version": 2, "journal_date": "2026-07-28",
        "sources": [{"text": DAY2_TEXT, "type": "pasted"}],
        "day": {"summary": "Dentist morning; planning the next climb.",
                "measurements": [{"metric": "mood", "numeric_value": 8}]},
        "entries": [
            {"kind": "task", "categories": {"primary": "health"},
             "title": "Book a dentist cleaning", "due_at": "2026-08-05",
             "provenance": {"char_start": 0, "char_end": 50}},
            {"kind": "plan", "categories": {"primary": "social"},
             "title": "Climb with Jake again",
             "entities": [{"ref": "new", "type": "person", "name": "Jake",
                           "role": "participant"}]},
        ]}))

    db = tmp_path / "healthconnect.db"
    make_hc_db(db)
    z = tmp_path / "hc_export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(db, "healthconnect.db")
    hc.apply_plan(conn, hc.build_plan(conn, hc.read_export(z, cfg)["records"]))

    chat = tmp_path / "conversations.json"
    chat.write_bytes((FIXTURES / "ai_chat_claude.json").read_bytes())
    _apply_scan(conn, ai_chat.scan(chat, cfg))

    ellis = entities.create(conn, "person", "Dr. Rebecca Ellis")
    _apply_scan(conn, _feed_scan(FEED))
    obs.link_handle(conn, "phone", JAKE, _entity_id(conn, "Jake"))
    obs.link_handle(conn, "email", ELLIS, ellis["id"])
    conn.commit()


@pytest.fixture
def seeded(conn, cfg, tmp_path):
    _seed(conn, cfg, tmp_path)
    ro = db_mod.connect(cfg.db_path, readonly=True)
    yield ro
    ro.close()


def _export(ro, vault, **kw):
    return export_obsidian(ro, vault, tz_name="America/Chicago", **kw)


def _read(vault, rel):
    return (vault / rel).read_text(encoding="utf-8")


def _obs_uuid(conn, stream, external_id):
    return conn.execute("SELECT uuid FROM observations WHERE stream=? AND "
                        "external_id=?", (stream, external_id)).fetchone()["uuid"]


def _snapshot(vault):
    return {p.relative_to(vault).as_posix(): p.read_bytes()
            for p in sorted(vault.rglob("*")) if p.is_file()}


# ---- the notes

def test_a_vault_has_day_entity_loops_and_index_notes(seeded, tmp_path):
    vault = tmp_path / "vault"
    result = _export(seeded, vault)
    for rel in ("Journal/2026/2026-07-27.md", "Journal/2026/2026-07-28.md",
                "Journal/2026/2026-03-10.md",      # an AI-chat-only day
                "People/Jake.md", "People/Dana.md", "People/Dr. Rebecca Ellis.md",
                "Places/Movement.md", "Activities/Bouldering.md",
                "Loops.md", "Index.md"):
        assert (vault / rel).is_file(), rel
    manifest = json.loads((vault / VAULT_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["format"] == "obsidian"
    assert manifest["files"] == sorted(
        p.relative_to(vault).as_posix() for p in vault.rglob("*.md"))
    assert result["files"] == len(manifest["files"])
    assert result["created"] == manifest["files"] and not result["kept"]


def test_every_generated_note_starts_with_the_marker_and_the_notice(seeded, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    for p in vault.rglob("*.md"):
        raw = p.read_bytes()
        assert raw.startswith(b"---\nlifecrm: generated\n"), p
        assert b"\r" not in raw, p                      # LF only, on Windows too
        text = raw.decode("utf-8")
        front, rest = text[4:].split("\n---\n", 1)
        assert rest.startswith(NOTICE + "\n"), p
        assert is_generated(p)


def test_a_day_note_carries_front_matter_entries_and_their_source(seeded, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    day = _read(vault, "Journal/2026/2026-07-27.md")
    front = day.split("\n---\n", 1)[0]
    assert "\ndate: 2026-07-27\n" in front
    assert "\nmood: 7\nenergy: 6\nsleep: 6.5\n" in front
    for tag in ("journal", "exercise", "food", "work", "idea", "social"):
        assert f'  - "{tag}"' in front, tag
    assert "# 2026-07-27 · Monday" in day
    assert "[[2026-07-28]] →" in day and "[[Index]]" in day
    assert "Shipped the quarterly report draft" in day           # summary
    assert "**Journal** · energy 6/10 · mood 7/10 · sleep_hours 6.5 h" in day

    assert "## exercise" in day and "### Bouldering at Movement with Jake" in day
    assert "`event` · evening · also social" in day
    assert "with [[Jake]] · at [[Movement]] · about [[Bouldering]]" in day
    assert "- duration: 90 min" in day
    assert "relates ← [[2026-07-27]] · App to track how often I call Mom" in day
    assert "closed: obsolete" in day
    # the transcript excerpt is a quote callout citing its source
    source_id = seeded.execute("SELECT id FROM sources WHERE journal_date="
                               "'2026-07-27'").fetchone()["id"]
    assert f"> [!quote] Source {source_id} · pasted · 2026-07-27\n" \
           f"> Okay, today was July 27th." in day
    assert "> Grabbed a chicken burrito bowl for lunch. After work I went " \
           "bouldering at Movement with Jake" in day       # segments 4-5
    uuid = seeded.execute("SELECT uuid FROM entries WHERE title LIKE "
                          "'Bouldering%'").fetchone()["uuid"]
    assert f"%%entry {uuid}%%" in day


def test_the_health_line_is_marked_observed_and_cites_its_export(seeded, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    d27 = _read(vault, "Journal/2026/2026-07-27.md")
    assert "**Health** · 6,000 steps · weight 84.5 kg — observed, from hc_export.zip" in d27
    d28 = _read(vault, "Journal/2026/2026-07-28.md")
    line = next(ln for ln in d28.splitlines() if ln.startswith("**Health**"))
    assert "sleep 6.5 h" in line and "8,432 steps" in line
    assert "1 workout (45 min)" in line and "also heart_rate, nutrition" in line
    assert line.endswith("— observed, from hc_export.zip")


def test_the_observed_digest_counts_streams_and_cites_every_item(seeded, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    day = _read(vault, "Journal/2026/2026-07-28.md")
    digest = day.split("## Observed", 1)[1]
    assert "calendar_event 1 · imessage 2 · note 1" in digest
    t2 = _obs_uuid(seeded, "imessage", "T2")
    assert (f"- 2 texts with [[Jake]] (imessage) · latest 18:12: "
            f"“yes! Veracruz after?” — observed `{t2}`") in digest
    c1 = _obs_uuid(seeded, "calendar_event", "C1")
    assert (f"- 09:00 Dentist — Dr. Ellis · with [[Dr. Rebecca Ellis]] — "
            f"observed `{c1}`") in digest
    n1 = _obs_uuid(seeded, "note", "N1")
    assert f"- note: Lake Bled packing list — observed `{n1}`" in digest
    # observed items never leak into the confirmed sections above the digest
    assert "Veracruz" not in day.split("## Observed", 1)[0]

    chat_day = _read(vault, "Journal/2026/2026-03-10.md")
    m1 = _obs_uuid(seeded, "claude_chat", "m1")
    assert f"- Claude chat: Slovenia trip planning (2 messages) — observed `{m1}`" \
        in chat_day
    assert "## exercise" not in chat_day
    # a day known only from imports is not tagged as a logged journal day
    assert 'tags:\n  - "observed"\n---' in chat_day
    assert '  - "journal"\n  - "observed"\n' in day


def test_the_digest_caps_notable_items_and_says_how_many_more(seeded, tmp_path):
    vault = tmp_path / "vault"
    export_obsidian(seeded, vault, tz_name="America/Chicago", notable=2)
    digest = _read(vault, "Journal/2026/2026-07-28.md").split("## Observed", 1)[1]
    items = [ln for ln in digest.splitlines() if ln.startswith("- ")]
    assert len(items) == 3 and items[-1] == "- … and 1 more"


def test_the_owner_is_never_listed_as_someone_they_texted(seeded, cfg, tmp_path):
    rw = db_mod.connect(cfg.db_path)
    me = entities.create(rw, "person", "Alex Rivera")
    obs.link_handle(rw, "phone", ME, me["id"])          # the feed's own account
    rw.commit()
    rw.close()
    vault = tmp_path / "vault"
    _export(seeded, vault)
    digest = _read(vault, "Journal/2026/2026-07-28.md").split("## Observed", 1)[1]
    assert "2 texts with [[Jake]]" in digest
    assert "Alex Rivera" not in digest
    # their own page still shows what their handle took part in
    assert "- [[2026-07-28]] — imessage 2" in _read(vault, "People/Alex Rivera.md")


def test_an_entity_note_has_aliases_days_handles_and_latest_contact(seeded, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    jake = _read(vault, "People/Jake.md")
    front = jake.split("\n---\n", 1)[0]
    uuid = seeded.execute("SELECT uuid FROM entities WHERE name='Jake'").fetchone()[0]
    assert '\ntype: "person"\n' in front and f'\nuuid: "{uuid}"\n' in front
    assert "\nfirst_seen: 2026-07-27\nlast_seen: 2026-07-28\n" in front
    assert "# Jake" in jake
    latest = jake.split("## Latest contact", 1)[1].split("##", 1)[0]
    t2 = _obs_uuid(seeded, "imessage", "T2")
    assert latest.strip().splitlines()[0] == (
        f"- observed · 2026-07-28 18:12 · imessage — “yes! Veracruz after?” · "
        f"[[2026-07-28]] · `{t2}`")
    assert "- journal · [[2026-07-28]] — Climb with Jake again" in latest
    assert ("- phone `+15125550142` · the source calls it “Jake M” · "
            "2 observations · 2026-07-28 … 2026-07-28 · linked ") in jake
    journal = jake.split("## Journal", 1)[1].split("##", 1)[0]
    assert journal.index("[[2026-07-28]]") < journal.index("[[2026-07-27]]")
    assert "Bouldering at Movement with Jake (participant)" in journal
    assert "- [[2026-07-28]] — imessage 2" in jake.split("## Observed", 1)[1]

    movement = _read(vault, "Places/Movement.md")
    assert 'aliases:\n  - "the gym"' in movement
    assert "## Latest\n" in movement and "## Handles" not in movement


def test_loops_lists_open_tasks_and_plans_soonest_due_first(seeded, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    loops = _read(vault, "Loops.md")
    task = seeded.execute("SELECT uuid FROM entries WHERE title LIKE 'Book%'"
                          ).fetchone()["uuid"]
    assert (f"- **2026-08-05** · Book a dentist cleaning `task` — from "
            f"[[2026-07-28]] · `{task}`") in loops
    assert "Climb with Jake again `plan` — since [[2026-07-28]] · with [[Jake]]" in loops
    assert loops.index("## Due") < loops.index("## No due date")
    assert "App to track" not in loops          # an idea, and closed anyway
    assert "- [ ]" not in loops                 # a checkbox would invite edits


def test_the_index_links_recent_days_people_places_and_loops(seeded, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    index = _read(vault, "Index.md")
    assert "3 day(s) (2026-03-10 … 2026-07-28) · [[Loops]]: 2 open" in index
    recent = index.split("## Recent days", 1)[1].split("##", 1)[0]
    assert recent.index("[[2026-07-28]]") < recent.index("[[2026-03-10]]")
    assert "- [[2026-07-28]] · Tuesday — Dentist morning; planning the next climb." in recent
    assert "- [[Jake]] — last seen [[2026-07-28]]" in index.split("## People", 1)[1]
    assert "- [[Movement]] — last seen [[2026-07-27]]" in index.split("## Places", 1)[1]
    assert "- observed · imessage: 2 row(s), 2026-07-28 … 2026-07-28" in index


# ---- determinism

def test_two_exports_of_the_same_database_are_byte_identical(seeded, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _export(seeded, a)
    _export(seeded, b)
    assert _snapshot(a) == _snapshot(b)
    assert build_vault(seeded, tz_name="America/Chicago") == \
        build_vault(seeded, tz_name="America/Chicago")


def test_a_re_export_with_nothing_new_writes_nothing(seeded, tmp_path):
    vault = tmp_path / "vault"
    first = _export(seeded, vault)
    stamp = {rel: os.stat(vault / rel).st_mtime_ns for rel in _snapshot(vault)}
    again = _export(seeded, vault)
    assert again["created"] == again["updated"] == again["deleted"] == []
    assert again["unchanged"] == first["files"]
    assert {rel: os.stat(vault / rel).st_mtime_ns for rel in stamp} == stamp


def test_the_export_reads_only(seeded, cfg, tmp_path):
    before = {t: seeded.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("audit_log", "entries", "observations", "recall_log")}
    _export(seeded, tmp_path / "vault")
    assert before == {t: seeded.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in before}
    rw = db_mod.connect(cfg.db_path)
    try:
        assert run_checks(rw, cfg)["failures"] == 0
    finally:
        rw.close()


# ---- the owner's vault

def test_the_owners_own_notes_are_never_touched(seeded, tmp_path):
    vault = tmp_path / "vault"
    mine = {"My ideas.md": "# ideas\n", "People/Jake.md": "# Jake — my own notes\n",
            "Journal/2026/reading list.md": "- Dune\n",
            ".obsidian/app.json": '{"theme": "dark"}',
            "Index.md": "---\ntags: [home]\n---\nmy own home page\n"}
    for rel, text in mine.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_bytes(text.encode("utf-8"))
    result = _export(seeded, vault)
    _export(seeded, vault)                      # and again, on top of itself
    for rel, text in mine.items():
        assert (vault / rel).read_bytes() == text.encode("utf-8"), rel
    assert {k["path"] for k in result["kept"]} == {"People/Jake.md", "Index.md"}
    listed = json.loads((vault / VAULT_MANIFEST).read_text(encoding="utf-8"))["files"]
    assert "People/Jake.md" not in listed and "Index.md" not in listed
    assert "Journal/2026/2026-07-27.md" in listed


def test_removing_the_marker_line_adopts_a_generated_note(seeded, cfg, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    dana = vault / "People" / "Dana.md"
    adopted = dana.read_text(encoding="utf-8").replace("lifecrm: generated\n", "") \
        + "\nMet her at the offsite.\n"
    dana.write_bytes(adopted.encode("utf-8"))
    rw = db_mod.connect(cfg.db_path)
    entities.rename(rw, _entity_id(rw, "Dana"), "Dana Reyes")   # Dana.md goes stale
    rw.commit()
    rw.close()
    result = _export(seeded, vault)
    assert dana.read_text(encoding="utf-8") == adopted
    assert "People/Dana.md" not in result["deleted"]
    assert any(k["path"] == "People/Dana.md" for k in result["kept"])
    assert (vault / "People" / "Dana Reyes.md").is_file()


def test_stale_generated_notes_are_removed_and_empty_folders_pruned(seeded, cfg, tmp_path):
    rw = db_mod.connect(cfg.db_path)
    old = _apply_scan(rw, _feed_scan(
        [_text("OLD1", "2019-03-03T18:00:00Z", "in", "happy birthday!",
               jd="2019-03-03")], connector="old_feed"))
    rw.commit()
    vault = tmp_path / "vault"
    _export(seeded, vault)
    assert (vault / "Journal/2019/2019-03-03.md").is_file()
    (vault / "Journal/2026/reading list.md").write_bytes(b"- Dune\n")

    obs.retract_import(rw, old["import_uuid"])
    obs.retract_import(rw, rw.execute("SELECT uuid FROM imports WHERE "
                                      "connector='ai_chat'").fetchone()["uuid"])
    rw.commit()
    rw.close()
    result = _export(seeded, vault)
    assert result["deleted"] == ["Journal/2019/2019-03-03.md",
                                 "Journal/2026/2026-03-10.md"]
    assert not (vault / "Journal/2019").exists()            # emptied, so pruned
    assert (vault / "Journal/2026/reading list.md").is_file()
    listed = json.loads((vault / VAULT_MANIFEST).read_text(encoding="utf-8"))["files"]
    assert "Journal/2026/2026-03-10.md" not in listed
    assert "[[2026-03-10]]" not in _read(vault, "Index.md")


def test_a_case_only_rename_ends_with_the_new_spelling(seeded, cfg, tmp_path):
    vault = tmp_path / "vault"
    _export(seeded, vault)
    rw = db_mod.connect(cfg.db_path)
    entities.rename(rw, _entity_id(rw, "Dana"), "dana")
    rw.commit()
    rw.close()
    result = _export(seeded, vault)
    assert result["deleted"] == ["People/Dana.md"]
    names = {p.name for p in (vault / "People").iterdir()}
    assert "dana.md" in names and "Dana.md" not in names
    assert _read(vault, "People/dana.md").splitlines()[0] == "---"


def test_dry_run_lists_what_would_change_and_writes_nothing(seeded, cfg, tmp_path):
    vault = tmp_path / "vault"
    preview = _export(seeded, vault, dry_run=True)
    assert not vault.exists()
    assert "Journal/2026/2026-07-27.md" in preview["created"]
    _export(seeded, vault)
    before = _snapshot(vault)

    rw = db_mod.connect(cfg.db_path)
    lunch = rw.execute("SELECT uuid FROM entries WHERE title LIKE 'Lunch%'"
                       ).fetchone()["uuid"]
    corrections.edit_entry(rw, lunch, {"title": "Lunch: carnitas burrito bowl"})
    rw.commit()
    rw.close()
    preview = _export(seeded, vault, dry_run=True)
    assert preview["updated"] == ["Journal/2026/2026-07-27.md"]
    assert preview["created"] == preview["deleted"] == []
    assert _snapshot(vault) == before


def test_a_corrupt_manifest_is_one_error_line(seeded, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / VAULT_MANIFEST).write_text("{not json", encoding="utf-8")
    with pytest.raises(DomainError) as e:
        _export(seeded, vault)
    assert VAULT_MANIFEST in str(e.value) and "\n" not in str(e.value)


def test_a_file_named_as_the_vault_is_refused(seeded, tmp_path):
    target = tmp_path / "vault.md"
    target.write_text("not a folder", encoding="utf-8")
    with pytest.raises(DomainError, match="must be a vault folder"):
        _export(seeded, target)


def test_a_manifest_cannot_reach_outside_the_vault(seeded, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("---\nlifecrm: generated\n---\nnot in the vault\n",
                       encoding="utf-8")
    (vault / VAULT_MANIFEST).write_text(json.dumps(
        {"files": ["../outside.md", str(outside), "C:/outside.md",
                   ".obsidian/workspace.md"]}), encoding="utf-8-sig")
    result = _export(seeded, vault)
    assert outside.is_file() and result["deleted"] == []


# ---- names

@pytest.mark.parametrize("name,stem", [
    ("AC/DC", "AC-DC"),
    ("con", "con_"),
    ("COM1", "COM1_"),
    ("nul.txt", "nul_.txt"),
    ("Dr. Who. ", "Dr. Who"),
    (".hidden", "hidden"),
    ("a#b^c[d]e|f", "a-b-c-d-e-f"),
    ('what? "really"', "what- -really-"),
    ("  ", "untitled"),
    ("Jose\u0301", "Jos\u00e9"),                  # NFD in, NFC out
])
def test_safe_stem_handles_reserved_names_and_characters(name, stem):
    assert safe_stem(name) == stem


@pytest.mark.parametrize("value,unit,text", [
    (7, "/10", "7/10"), (6.5, "h", "6.5 h"), (10432, "steps", "10,432 steps"),
    (1234567, "", "1234567"), (84.25, "kg", "84.25 kg"),
])
def test_amounts_read_the_way_a_person_writes_them(value, unit, text):
    assert _amount(value, unit) == text


def test_long_names_are_cut_to_a_portable_length():
    assert len(safe_stem("x" * 300)) == 80


def test_colliding_names_are_disambiguated_by_type_then_uuid():
    stems = unique_stems([
        (1, "Jake", "person", "aaaaaaaa-1"), (2, "jake", "topic", "bbbbbbbb-2"),
        (3, "AC/DC", "media", "cccccccc-3"), (4, "AC-DC", "media", "dddddddd-4"),
        (5, "Index", "topic", "eeeeeeee-5"), (6, "Movement", "place", "ffffffff-6"),
    ], fixed={"index", "loops"})
    assert stems == {1: "Jake (person)", 2: "jake (topic)",
                     3: "AC-DC (media cccccccc)", 4: "AC-DC (media dddddddd)",
                     5: "Index (topic)", 6: "Movement"}
    # order-independent: the same collisions resolve the same way reversed
    assert unique_stems([(6, "Movement", "place", "ffffffff-6"),
                         (2, "jake", "topic", "bbbbbbbb-2"),
                         (1, "Jake", "person", "aaaaaaaa-1")],
                        fixed=set()) == {6: "Movement", 2: "jake (topic)",
                                         1: "Jake (person)"}


def test_disambiguated_names_link_with_their_display_name(seeded, cfg, tmp_path):
    rw = db_mod.connect(cfg.db_path)
    entities.create(rw, "topic", "jake")
    entities.create(rw, "organization", "CON")
    entities.create(rw, "topic", "2026-07-27")
    rw.commit()
    rw.close()
    vault = tmp_path / "vault"
    _export(seeded, vault)
    assert (vault / "People/Jake (person).md").is_file()
    assert (vault / "Topics/jake (topic).md").is_file()
    assert (vault / "Organizations/CON_.md").is_file()
    assert (vault / "Topics/2026-07-27 (topic).md").is_file()
    day = _read(vault, "Journal/2026/2026-07-27.md")
    assert "with [[Jake (person)|Jake]]" in day
    jake = _read(vault, "People/Jake (person).md")
    assert 'aliases:\n  - "Jake"' in jake                     # findable by name
    con = _read(vault, "Organizations/CON_.md")
    assert '\naliases:\n  - "CON"\n' in con


# ---- CLI

def test_cli_export_obsidian_previews_then_writes(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    conn.commit()
    conn.close()
    vault = tmp_path / "vault"

    r = runner.invoke(app, ["export", "--format", "obsidian", "--out", str(vault),
                            "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "  + Journal/2026/2026-07-27.md" in r.output
    assert "dry run — nothing written" in r.output
    assert not vault.exists()

    r = runner.invoke(app, ["export", "--format", "obsidian", "--out", str(vault)])
    assert r.exit_code == 0, r.output
    assert "0 changed, 0 unchanged, 0 removed" in r.output
    assert "  + " not in r.output
    r = runner.invoke(app, ["export", "--format", "obsidian", "--out", str(vault)])
    assert "— 0 new, 0 changed" in r.output

    r = runner.invoke(app, ["export", "--format", "jsonl", "--out",
                            str(tmp_path / "j"), "--dry-run"])
    assert r.exit_code == 1 and "--dry-run applies to --format obsidian" in r.output
    r = runner.invoke(app, ["export", "--format", "pdf", "--out", str(tmp_path / "p")])
    assert r.exit_code == 1 and "jsonl, markdown or obsidian" in r.output


def test_cli_reports_a_corrupt_manifest_as_one_error_line(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / VAULT_MANIFEST).write_text("[]", encoding="utf-8")
    r = runner.invoke(app, ["export", "--format", "obsidian", "--out", str(vault)])
    assert r.exit_code == 1
    assert r.output.startswith("error: ") and "no 'files' list" in r.output

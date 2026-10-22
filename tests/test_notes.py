"""Notes connector: a folder of markdown/text notes and a Google Keep Takeout.

The connector is a pure reader, so most of this exercises scan() directly
against inputs built in tmp_path, shaped like the real thing — an Obsidian
vault with its settings and trash folders, front matter in the forms people
actually write, files saved by Notepad with a BOM and CRLF, iCloud
placeholders, and Keep's per-note JSON with checklists, labels, sharing and
microsecond timestamps. Mirroring is tested the way the owner will meet it:
a note deleted at the source leaves the archive, and nothing else does.

Apple Notes' NoteStore.sqlite has its own file (test_notes_apple.py); the
demo samples theirs (test_notes_demo.py).
"""

import json
import os
import zipfile
from datetime import datetime, timezone

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import base, notes
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks

runner = CliRunner()

# 2026-10-01 17:30:15.75 UTC = 12:30 in Chicago: an ordinary afternoon edit.
MTIME = datetime(2026, 10, 1, 17, 30, 15, 750000, tzinfo=timezone.utc).timestamp()


def write_note(folder, rel, text, mtime=MTIME, *, raw=None):
    p = folder / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw if raw is not None else text.encode("utf-8"))
    os.utime(p, (mtime, mtime))
    return p


def make_vault(root):
    """An Obsidian vault with every kind of file a real one holds."""
    root.mkdir(parents=True, exist_ok=True)
    write_note(root, ".obsidian/app.json", '{"theme": "dark"}')
    write_note(root, ".obsidian/notes-in-settings.md", "# not a note")
    write_note(root, ".trash/Deleted idea.md", "# Deleted idea\nthrown away")
    write_note(root, ".scratch.md", "a dotfile")
    write_note(root, "Inbox.md", "# Weekend plans\n\n- climb on Saturday\n## Later\nmore")
    write_note(root, "Trips/Bled.md",
               "---\ntitle: Lake Bled trip\ncreated: 2022-09-26\n"
               "tags: [travel, slovenia]\naliases:\n  - Bled\n  - Slovenia trip\n---\n"
               "# Packing\nrain jacket, hiking boots\n")
    write_note(root, "Daily/2026-10-01.md",
               "---\ncreated: 2026-10-01 07:45\nmood: 7\n---\nSlept well, gym at 6:30.")
    write_note(root, "Plain.txt", "Remember to call Mom on Sunday")
    write_note(root, "attachments/photo.png", "", raw=b"\x89PNG\r\n")
    write_note(root, "Empty.md", "  \n\n")
    write_note(root, "Latin1.md", "", raw=b"caf\xe9 au lait")
    return root


def _by_id(scan):
    return {r["external_id"]: r for r in scan["records"]}


def _apply(conn, scan, **kw):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan, **kw)
    conn.commit()
    return plan, result


def _active_ids(conn):
    return sorted(r["external_id"] for r in conn.execute(
        "SELECT external_id FROM observations WHERE stream='note' AND status='active'"))


def fid(folder, rel):
    """A folder note's id: the folder's key — its name and a fingerprint of
    where it is — and the note's path inside it."""
    return f"file:{notes.folder_key(folder.resolve())}/{rel}"


# ---------------------------------------------------------------- folder: shape

def test_a_vault_scan_reads_notes_and_skips_settings_trash_and_dotfiles(tmp_path, cfg):
    vault = tmp_path / "Vault"
    scan = notes.scan(make_vault(vault), cfg)
    assert scan["connector"] == "notes"
    assert set(_by_id(scan)) == {fid(vault, "Inbox.md"), fid(vault, "Trips/Bled.md"),
                                 fid(vault, "Daily/2026-10-01.md"), fid(vault, "Plain.txt")}
    assert all(i.startswith("file:Vault~") for i in _by_id(scan))
    assert all(r["stream"] == "note" for r in scan["records"])
    assert {r["origin_app"] for r in scan["records"]} == {"obsidian"}
    assert scan["source_kind"] == "obsidian"
    assert scan["account"] == str((tmp_path / "Vault").resolve())


def test_a_plain_folder_is_a_notes_folder_not_a_vault(tmp_path, cfg):
    write_note(tmp_path / "Samsung Notes", "Shopping.txt", "milk, eggs")
    scan = notes.scan(tmp_path / "Samsung Notes", cfg)
    assert scan["records"][0]["origin_app"] == "notes_folder"
    assert scan["records"][0]["external_id"] == fid(tmp_path / "Samsung Notes", "Shopping.txt")


def test_title_comes_from_front_matter_then_a_leading_heading_then_the_file_name(tmp_path, cfg):
    vault = tmp_path / "Vault"
    recs = _by_id(notes.scan(make_vault(vault), cfg))
    assert recs[fid(vault, "Trips/Bled.md")]["title"] == "Lake Bled trip"
    assert recs[fid(vault, "Inbox.md")]["title"] == "Weekend plans"
    assert recs[fid(vault, "Plain.txt")]["title"] == "Plain"
    assert recs[fid(vault, "Daily/2026-10-01.md")]["title"] == "2026-10-01"


def test_a_heading_further_down_is_a_section_not_the_title(tmp_path, cfg):
    write_note(tmp_path / "v", "Bled trip.md", "Notes from the trip\n\n## Packing\nboots")
    rec = notes.scan(tmp_path / "v", cfg)["records"][0]
    assert rec["title"] == "Bled trip"


def test_front_matter_is_stripped_from_the_body_into_value_json(tmp_path, cfg):
    vault = tmp_path / "Vault"
    rec = _by_id(notes.scan(make_vault(vault), cfg))[fid(vault, "Trips/Bled.md")]
    assert rec["body"] == "# Packing\nrain jacket, hiking boots"
    detail = json.loads(rec["value_json"])
    assert detail["front_matter"] == {"title": "Lake Bled trip", "created": "2022-09-26",
                                      "tags": ["travel", "slovenia"],
                                      "aliases": ["Bled", "Slovenia trip"]}
    assert detail["path"] == "Trips/Bled.md" and detail["format"] == "markdown"
    assert detail["source"] == "folder"


def test_a_text_file_keeps_dashes_at_the_top_as_text(tmp_path, cfg):
    """Front matter is a markdown convention; a .txt that starts with '---'
    is just text."""
    write_note(tmp_path / "v", "rule.txt", "---\ntitle: x\n---\nbody")
    rec = notes.scan(tmp_path / "v", cfg)["records"][0]
    assert rec["body"].startswith("---\ntitle: x")
    assert "front_matter" not in json.loads(rec["value_json"])


def test_an_unclosed_rule_is_not_front_matter(tmp_path, cfg):
    write_note(tmp_path / "v", "a.md", "---\nkey: value\nno closing fence")
    rec = notes.scan(tmp_path / "v", cfg)["records"][0]
    assert rec["body"].startswith("---")
    assert "front_matter" not in json.loads(rec["value_json"])


# ---------------------------------------------------------------- folder: time

def test_a_bare_front_matter_date_stays_on_its_own_journal_day(tmp_path, cfg):
    """Local midnight would fall before the 05:00 rollover and file the note
    on the day before; a bare date is read as local noon instead."""
    vault = tmp_path / "Vault"
    rec = _by_id(notes.scan(make_vault(vault), cfg))[fid(vault, "Trips/Bled.md")]
    assert rec["started_at"] == "2022-09-26T17:00:00Z"        # noon CDT
    assert rec["journal_date"] == "2022-09-26"


def test_a_front_matter_time_without_a_zone_is_local_time(tmp_path, cfg):
    vault = tmp_path / "Vault"
    rec = _by_id(notes.scan(make_vault(vault), cfg))[fid(vault, "Daily/2026-10-01.md")]
    assert rec["started_at"] == "2026-10-01T12:45:00Z"        # 07:45 CDT
    assert rec["journal_date"] == "2026-10-01"


def test_a_front_matter_time_with_an_offset_is_honoured(tmp_path, cfg):
    write_note(tmp_path / "v", "a.md", "---\ncreated: 2026-07-14T09:00:00+02:00\n---\nhi")
    assert notes.scan(tmp_path / "v", cfg)["records"][0]["started_at"] == "2026-07-14T07:00:00Z"


def test_without_a_created_date_the_modified_time_dates_the_note(tmp_path, cfg):
    vault = tmp_path / "Vault"
    rec = _by_id(notes.scan(make_vault(vault), cfg))[fid(vault, "Inbox.md")]
    assert rec["started_at"] == "2026-10-01T17:30:15Z"        # sub-second dropped
    assert rec["last_modified_at"] == "2026-10-01T17:30:15Z"
    assert rec["journal_date"] == "2026-10-01"


def test_a_late_night_edit_belongs_to_the_previous_journal_day(tmp_path, cfg):
    """06:30 UTC is 01:30 in Chicago — before the rollover hour."""
    late = datetime(2026, 10, 2, 6, 30, tzinfo=timezone.utc).timestamp()
    write_note(tmp_path / "v", "late.md", "couldn't sleep", mtime=late)
    rec = notes.scan(tmp_path / "v", cfg)["records"][0]
    assert rec["started_at"] == "2026-10-02T06:30:00Z"
    assert rec["journal_date"] == "2026-10-01"


def test_a_front_matter_date_it_cannot_read_falls_back_and_says_so(tmp_path, cfg):
    write_note(tmp_path / "v", "a.md", "---\ncreated: last Tuesday\n---\nhi")
    scan = notes.scan(tmp_path / "v", cfg)
    assert scan["records"][0]["started_at"] == "2026-10-01T17:30:15Z"
    assert scan["unmapped"] == [{"what": "front-matter date not understood (dated by "
                                         "the file's modified time)", "count": 1}]


# ---------------------------------------------------------------- folder: encodings

def test_a_bom_and_crlf_line_endings_do_not_hide_front_matter(tmp_path, cfg):
    """Notepad saves UTF-8 with a BOM and CRLF; the '---' must still open
    the front matter."""
    text = "---\r\ntitle: From Windows\r\n---\r\nline one\r\nline two\r\n"
    write_note(tmp_path / "v", "win.md", "", raw=b"\xef\xbb\xbf" + text.encode("utf-8"))
    rec = notes.scan(tmp_path / "v", cfg)["records"][0]
    assert rec["title"] == "From Windows"
    assert rec["body"] == "line one\nline two"


def test_utf16_with_a_bom_is_read(tmp_path, cfg):
    write_note(tmp_path / "v", "u.txt", "", raw="Bled — café".encode("utf-16"))
    assert notes.scan(tmp_path / "v", cfg)["records"][0]["body"] == "Bled — café"


def test_skips_are_counted_not_hidden(tmp_path, cfg):
    scan = notes.scan(make_vault(tmp_path / "Vault"), cfg)
    assert "1 × empty note" in scan["skipped"]
    assert "1 × note file that is not UTF-8 text" in scan["skipped"]
    assert {"what": ".png file (not a note)", "count": 1} in scan["unmapped"]
    assert scan["notes_seen"] == 6


def test_a_note_it_cannot_read_still_counts_as_present(tmp_path, cfg):
    vault = tmp_path / "Vault"
    scan = notes.scan(make_vault(vault), cfg)
    assert fid(vault, "Latin1.md") in scan["present_ids"]
    assert fid(vault, "Empty.md") in scan["present_ids"]
    assert fid(vault, "Latin1.md") not in _by_id(scan)


def test_an_icloud_placeholder_is_present_but_not_imported(tmp_path, cfg):
    """'Optimize Mac Storage' evicts a note to '.<name>.icloud'. The note is
    still in the folder — mirroring must not read it as deleted."""
    write_note(tmp_path / "v", "Here.md", "downloaded")
    write_note(tmp_path / "v", "Trips/.Bled.md.icloud", "", raw=b"bplist00")
    scan = notes.scan(tmp_path / "v", cfg)
    assert fid(tmp_path / "v", "Trips/Bled.md") in scan["present_ids"]
    assert set(_by_id(scan)) == {fid(tmp_path / "v", "Here.md")}
    assert any("not downloaded from iCloud" in s for s in scan["skipped"])


def test_two_scans_of_an_unchanged_folder_are_identical(tmp_path, cfg):
    vault = make_vault(tmp_path / "Vault")
    a, b = notes.scan(vault, cfg), notes.scan(vault, cfg)
    assert a["records"] == b["records"]
    assert a["source_hash"] == b["source_hash"] and a["source_hash"]
    write_note(vault, "Plain.txt", "changed")
    assert notes.scan(vault, cfg)["source_hash"] != a["source_hash"]


def test_a_folder_with_no_notes_fails_naming_what_it_holds(tmp_path, cfg):
    write_note(tmp_path / "pics", "a.jpg", "", raw=b"\xff\xd8")
    with pytest.raises(DomainError, match=r"holds no \.md or \.txt notes .*a\.jpg"):
        notes.scan(tmp_path / "pics", cfg)


def test_a_single_note_file_is_refused_with_the_fix(tmp_path, cfg):
    p = write_note(tmp_path, "one.md", "hello")
    with pytest.raises(DomainError, match="pass the folder"):
        notes.scan(p, cfg)


def test_a_missing_target_says_not_found(tmp_path, cfg):
    with pytest.raises(DomainError, match="not found"):
        notes.scan(tmp_path / "nope", cfg)


def test_permission_denied_becomes_one_line_naming_full_disk_access(tmp_path, cfg, monkeypatch):
    def denied(folder, cfg):
        raise PermissionError(13, "Operation not permitted", "/Users/alex/Documents/Notes")
    monkeypatch.setattr(notes, "_read_folder", denied)
    write_note(tmp_path / "v", "a.md", "x")
    with pytest.raises(DomainError, match="Full Disk Access") as e:
        notes.scan(tmp_path / "v", cfg)
    assert "\n" not in str(e.value)


# ---------------------------------------------------------------- folder: report, apply

def test_report_lines_show_counts_span_and_sample(tmp_path, cfg, conn):
    scan = notes.scan(make_vault(tmp_path / "Vault"), cfg)
    text = "\n".join(notes.report_lines(scan, obs.build_plan(conn, scan)))
    assert "notes: Vault (Obsidian vault; 6 note(s) in the source)" in text
    assert "note: 4 new, 0 changed | 2022-09-26..2026-10-01" in text
    assert "skipped: 1 × empty note" in text
    assert "not imported: 1 × .png file (not a note)" in text


def test_apply_then_reapply_is_identical_and_touches_no_confirmed_table(tmp_path, cfg, conn):
    vault = make_vault(tmp_path / "Vault")
    _, first = _apply(conn, notes.scan(vault, cfg))
    assert first["added"] == 4
    plan, again = _apply(conn, notes.scan(vault, cfg))
    assert again["added"] == 0 and again["identical"] == 4 and plan["removed"] == []
    for table in ("entries", "measurements", "mentions", "entity_aliases",
                  "entry_sources", "extraction_runs"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0
    assert run_checks(conn, cfg)["failures"] == 0


def test_an_edited_note_updates_through_newer_wins(tmp_path, cfg, conn):
    vault = make_vault(tmp_path / "Vault")
    _apply(conn, notes.scan(vault, cfg))
    later = MTIME + 3600
    write_note(vault, "Trips/Bled.md",
               "---\ntitle: Lake Bled trip\ncreated: 2022-09-26\n---\n# Packing\nrain jacket",
               mtime=later)
    plan, result = _apply(conn, notes.scan(vault, cfg))
    assert result["updated"] == 1
    row = conn.execute("SELECT body, started_at, last_modified_at FROM observations "
                       "WHERE external_id=?", (fid(vault, "Trips/Bled.md"),)).fetchone()
    assert row["body"] == "# Packing\nrain jacket"
    assert row["started_at"] == "2022-09-26T17:00:00Z"        # still when it was written
    assert row["last_modified_at"] == "2026-10-01T18:30:15Z"


def test_an_older_copy_of_a_note_is_a_conflict_not_an_overwrite(tmp_path, cfg, conn):
    vault = make_vault(tmp_path / "Vault")
    _apply(conn, notes.scan(vault, cfg))
    write_note(vault, "Plain.txt", "restored from an old backup", mtime=MTIME - 86400)
    plan = obs.build_plan(conn, notes.scan(vault, cfg))
    # Without a created date the mtime also dates it, so started_at differs too.
    assert [c["external_id"] for c in plan["conflicts"]] == [fid(vault, "Plain.txt")]


def test_two_folders_holding_the_same_file_name_stay_apart(tmp_path, cfg, conn):
    write_note(tmp_path / "Work", "Inbox.md", "work inbox")
    write_note(tmp_path / "Home", "Inbox.md", "home inbox")
    _apply(conn, notes.scan(tmp_path / "Work", cfg))
    plan, result = _apply(conn, notes.scan(tmp_path / "Home", cfg))
    assert result["added"] == 1 and plan["conflicts"] == [] and plan["updates"] == []
    assert _active_ids(conn) == [fid(tmp_path / "Home", "Inbox.md"),
                                 fid(tmp_path / "Work", "Inbox.md")]


# ---------------------------------------------------------------- folder: mirroring
# The archive mirrors the source: a note deleted from the folder — or moved
# into Obsidian's .trash — leaves on the next import. Nothing else does.

def test_a_note_deleted_from_the_folder_is_removed(tmp_path, cfg, conn):
    vault = make_vault(tmp_path / "Vault")
    _apply(conn, notes.scan(vault, cfg))
    (vault / "Plain.txt").unlink()
    plan, result = _apply(conn, notes.scan(vault, cfg))
    assert [r["external_id"] for r in plan["removed"]] == [fid(vault, "Plain.txt")]
    assert result["removed"] == 1
    assert fid(vault, "Plain.txt") not in _active_ids(conn)


def test_a_note_moved_to_obsidians_trash_is_removed(tmp_path, cfg, conn):
    vault = make_vault(tmp_path / "Vault")
    _apply(conn, notes.scan(vault, cfg))
    (vault / "Inbox.md").rename(vault / ".trash" / "Inbox.md")
    plan, _ = _apply(conn, notes.scan(vault, cfg))
    assert [r["external_id"] for r in plan["removed"]] == [fid(vault, "Inbox.md")]
    text = "\n".join(notes.report_lines(notes.scan(vault, cfg),
                                        obs.build_plan(conn, notes.scan(vault, cfg))))
    assert "nothing new" in text


def test_a_note_that_turns_unreadable_is_not_mistaken_for_a_deletion(tmp_path, cfg, conn):
    vault = make_vault(tmp_path / "Vault")
    _apply(conn, notes.scan(vault, cfg))
    write_note(vault, "Plain.txt", "", raw=b"\xff\xfe\x00\xd8broken utf-16", mtime=MTIME + 60)
    scan = notes.scan(vault, cfg)
    assert fid(vault, "Plain.txt") not in _by_id(scan)
    assert obs.build_plan(conn, scan)["removed"] == []


def test_another_folders_notes_are_never_removed(tmp_path, cfg, conn):
    write_note(tmp_path / "A", "a.md", "in A")
    write_note(tmp_path / "B", "b.md", "in B")
    _apply(conn, notes.scan(tmp_path / "A", cfg))
    plan, _ = _apply(conn, notes.scan(tmp_path / "B", cfg))
    assert plan["removed"] == []
    assert _active_ids(conn) == [fid(tmp_path / "A", "a.md"), fid(tmp_path / "B", "b.md")]


def test_a_mass_removal_is_refused(tmp_path, cfg, conn, monkeypatch):
    monkeypatch.setattr(obs, "MASS_REMOVAL_MIN", 2)
    for i in range(6):
        write_note(tmp_path / "v", f"n{i}.md", f"note {i}")
    _apply(conn, notes.scan(tmp_path / "v", cfg))
    for i in range(5):
        (tmp_path / "v" / f"n{i}.md").unlink()
    scan = notes.scan(tmp_path / "v", cfg)
    plan = obs.build_plan(conn, scan)
    assert len(plan["removed"]) == 5 and obs.mass_removal(plan)
    with pytest.raises(DomainError, match="allow-mass-removal"):
        obs.apply_plan(conn, scan, plan)
    conn.rollback()
    assert len(_active_ids(conn)) == 6


# ---------------------------------------------------------------- Google Keep

def usec(*when, extra=0):
    """Keep's timestamp: microseconds since the epoch."""
    return int(datetime(*when, tzinfo=timezone.utc).timestamp()) * 1_000_000 + extra


KEEP = {
    "Gift ideas.json": {
        "color": "DEFAULT", "isTrashed": False, "isPinned": True, "isArchived": False,
        "title": "Gift ideas",
        "userEditedTimestampUsec": usec(2026, 10, 3, 3, 20, extra=999_999),
        "createdTimestampUsec": usec(2026, 9, 21, 13, 33, 20, extra=123_456),
        "labels": [{"name": "personal"}, {"name": "birthdays"}],
        "listContent": [{"text": "ceramics class voucher", "isChecked": True},
                        {"text": "concert tickets", "isChecked": False}]},
    "Deck outline.json": {
        "color": "BLUE", "isTrashed": False, "isPinned": False, "isArchived": True,
        "title": "Deck outline", "textContent": "1. Customer story\n2. Metrics",
        "userEditedTimestampUsec": 1790500000000000, "createdTimestampUsec": 1790400000000000,
        "annotations": [{"description": "", "source": "WEBLINK", "title": "Pilot",
                         "url": "https://example.com/pilot"}],
        "attachments": [{"filePath": "abc123.png", "mimetype": "image/png"}],
        "sharees": [{"isOwner": True, "type": "WRITER", "email": "Alex@Example.com"},
                    {"isOwner": False, "type": "WRITER", "email": "priya@acme.example"}]},
    "Packing.json": {
        "isTrashed": False, "title": "Packing", "textContent": "boots",
        "userEditedTimestampUsec": 1790600000000000,
        "sharees": [{"isOwner": True, "type": "WRITER", "email": "alex@example.com"},
                    {"isOwner": False, "type": "WRITER", "email": "jake@example.com"}]},
    "Old list.json": {
        "isTrashed": True, "title": "Old list", "textContent": "eggs",
        "userEditedTimestampUsec": 1780000000000000, "createdTimestampUsec": 1780000000000000},
    "2026-10-05T12_00_00.000-05_00.json": {
        "isTrashed": False, "title": "", "textContent": "untitled thought",
        "userEditedTimestampUsec": 1791230400000000, "createdTimestampUsec": 1791230400000000},
}


def make_keep(root, notes_=None, *, as_zip=True, name="takeout-20261021T120000Z-001.zip",
              extra=None):
    """A Keep-only Takeout: Takeout/Keep/<note>.json (+ .html and Labels.txt,
    which the importer ignores)."""
    notes_ = dict(KEEP if notes_ is None else notes_)
    files = {f"Takeout/Keep/{k}": json.dumps(v).encode("utf-8") for k, v in notes_.items()}
    files["Takeout/Keep/Labels.txt"] = b"personal\nbirthdays\n"
    files["Takeout/Keep/Gift ideas.html"] = b"<html></html>"
    files["Takeout/archive_browser.html"] = b"<html></html>"
    files.update(extra or {})
    root.mkdir(parents=True, exist_ok=True)
    if as_zip:
        z = root / name
        with zipfile.ZipFile(z, "w") as zf:
            for k, v in files.items():
                zf.writestr(k, v)
        return z
    for k, v in files.items():
        p = root / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(v)
    return root


def test_keep_scan_shape_from_the_takeout_zip(tmp_path, cfg):
    scan = notes.scan(make_keep(tmp_path), cfg)
    assert scan["source_kind"] == "keep"
    assert set(_by_id(scan)) == {"keep:Gift ideas", "keep:Deck outline", "keep:Packing",
                                 "keep:2026-10-05T12_00_00.000-05_00"}
    assert {r["origin_app"] for r in scan["records"]} == {"google_keep"}


@pytest.mark.parametrize("target", ["root", "Takeout", "Keep"])
def test_an_unpacked_takeout_reads_the_same_as_the_zip(tmp_path, cfg, target):
    zipped = notes.scan(make_keep(tmp_path / "z"), cfg)
    root = make_keep(tmp_path / "u", as_zip=False)
    where = {"root": root, "Takeout": root / "Takeout", "Keep": root / "Takeout" / "Keep"}
    unpacked = notes.scan(where[target], cfg)
    assert unpacked["records"] == zipped["records"]
    assert unpacked["present_ids"] == zipped["present_ids"]


def test_keep_checklists_read_as_markdown_task_lists(tmp_path, cfg):
    rec = _by_id(notes.scan(make_keep(tmp_path), cfg))["keep:Gift ideas"]
    assert rec["body"] == "- [x] ceramics class voucher\n- [ ] concert tickets"
    detail = json.loads(rec["value_json"])
    assert detail["checklist"] == {"items": 2, "checked": 1}
    assert detail["labels"] == ["birthdays", "personal"]
    assert detail["pinned"] is True and detail["file"] == "Gift ideas.json"


def test_keep_microseconds_become_whole_seconds(tmp_path, cfg):
    recs = _by_id(notes.scan(make_keep(tmp_path), cfg))
    assert recs["keep:Gift ideas"]["started_at"] == "2026-09-21T13:33:20Z"
    assert recs["keep:Gift ideas"]["last_modified_at"] == "2026-10-03T03:20:00Z"
    assert recs["keep:Gift ideas"]["journal_date"] == "2026-09-21"
    # no createdTimestampUsec (older exports): the last edit dates the note
    assert recs["keep:Packing"]["started_at"] == recs["keep:Packing"]["last_modified_at"]


def test_an_impossible_keep_timestamp_falls_back_or_is_counted(tmp_path, cfg):
    far = 10 ** 30
    z = make_keep(tmp_path, {
        "Fallback.json": {"title": "Fallback", "textContent": "x",
                          "createdTimestampUsec": far,
                          "userEditedTimestampUsec": usec(2026, 5, 1, 12)},
        "Hopeless.json": {"title": "Hopeless", "textContent": "y",
                          "userEditedTimestampUsec": far}})
    scan = notes.scan(z, cfg)
    assert _by_id(scan)["keep:Fallback"]["started_at"] == "2026-05-01T12:00:00Z"
    assert "1 × Keep note with no usable timestamp" in scan["skipped"]


def test_keep_archived_notes_are_kept_and_flagged_trashed_ones_are_not(tmp_path, cfg):
    scan = notes.scan(make_keep(tmp_path), cfg)
    detail = json.loads(_by_id(scan)["keep:Deck outline"]["value_json"])
    assert detail["archived"] is True and detail["color"] == "BLUE"
    assert detail["links"] == [{"title": "Pilot", "url": "https://example.com/pilot"}]
    assert detail["attachments"] == [{"file": "abc123.png", "mime": "image/png"}]
    assert "keep:Old list" not in _by_id(scan)
    assert "keep:Old list" not in scan["present_ids"]          # trashed = deleted
    assert "1 × note in Keep's trash (deleted — not kept)" in scan["skipped"]


def test_an_untitled_keep_note_has_no_title_but_keeps_its_text(tmp_path, cfg):
    rec = _by_id(notes.scan(make_keep(tmp_path), cfg))["keep:2026-10-05T12_00_00.000-05_00"]
    assert rec["title"] is None and rec["body"] == "untitled thought"


def test_keep_sharees_are_email_handles_never_names(tmp_path, cfg):
    scan = notes.scan(make_keep(tmp_path), cfg)
    assert scan["handles"] == [
        {"kind": "email", "value": "jake@example.com", "display_name": None},
        {"kind": "email", "value": "priya@acme.example", "display_name": None}]
    rec = _by_id(scan)["keep:Deck outline"]
    assert rec["participants"] == [{"kind": "email", "value": "priya@acme.example",
                                    "role": "participant"}]


def test_keep_account_is_the_one_address_on_every_shared_note(tmp_path, cfg):
    scan = notes.scan(make_keep(tmp_path), cfg)
    assert scan["account"] == "alex@example.com"               # lower-cased
    assert "every shared note" in scan["account_from"]


def test_keep_account_from_config_wins(tmp_path, cfg):
    cfg.connectors = {"notes": {"keep_account": "Me@Gmail.com"}}
    scan = notes.scan(make_keep(tmp_path), cfg)
    assert scan["account"] == "me@gmail.com"


def test_a_keep_export_that_names_no_account_removes_nothing(tmp_path, cfg, conn):
    unshared = {k: {kk: vv for kk, vv in v.items() if kk != "sharees"}
                for k, v in KEEP.items()}
    _apply(conn, notes.scan(make_keep(tmp_path / "a", unshared), cfg))
    later = {k: v for k, v in unshared.items() if k != "Packing.json"}
    scan = notes.scan(make_keep(tmp_path / "b", later), cfg)
    assert scan["account"] is None
    plan = obs.build_plan(conn, scan)
    assert plan["removed"] == [] and "does not say which account" in plan["mirror_note"]
    text = "\n".join(notes.report_lines(scan, plan))
    assert "not mirroring deletions" in text and "keep_account" in text


def test_trashing_a_shared_note_does_not_lose_the_account(tmp_path, cfg):
    """The trashed note still names Alex; without it only the deck outline
    (Alex and Priya) would be left, and mirroring would quietly stop."""
    later = {**KEEP, "Packing.json": {**KEEP["Packing.json"], "isTrashed": True}}
    scan = notes.scan(make_keep(tmp_path, later), cfg)
    assert scan["account"] == "alex@example.com"
    assert {"kind": "email", "value": "jake@example.com", "display_name": None} \
        not in scan["handles"]                     # a trashed note contributes no handle


def test_a_single_shared_note_is_not_enough_to_name_the_account(tmp_path, cfg):
    """Alex and Jake are both on it; either could be the export's owner."""
    one = {"Packing.json": KEEP["Packing.json"]}
    assert notes.scan(make_keep(tmp_path, one), cfg)["account"] is None


def test_a_keep_file_that_is_not_json_is_skipped_but_present(tmp_path, cfg):
    z = make_keep(tmp_path, extra={"Takeout/Keep/Broken.json": b"{not json"})
    scan = notes.scan(z, cfg)
    assert "keep:Broken" in scan["present_ids"]
    assert "1 × Keep note file that is not valid JSON" in scan["skipped"]


def test_a_json_in_keep_that_is_not_a_note_is_reported(tmp_path, cfg):
    z = make_keep(tmp_path, extra={"Takeout/Keep/settings.json": b'{"theme": 1}'})
    scan = notes.scan(z, cfg)
    assert {"what": "JSON file in Keep/ that is not a note", "count": 1} in scan["unmapped"]
    assert "keep:settings" not in scan["present_ids"]


def test_keep_json_with_a_bom_is_read(tmp_path, cfg):
    note = json.dumps(KEEP["Packing.json"]).encode("utf-8")
    z = make_keep(tmp_path, {}, extra={"Takeout/Keep/Packing.json": b"\xef\xbb\xbf" + note})
    assert set(_by_id(notes.scan(z, cfg))) == {"keep:Packing"}


def test_a_zip_without_keep_notes_fails_naming_its_members(tmp_path, cfg):
    z = tmp_path / "takeout-x-001.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/Calendar/me.ics", "BEGIN:VCALENDAR")
    with pytest.raises(DomainError, match=r"no Takeout/Keep/\*\.json notes.*Calendar"):
        notes.scan(z, cfg)


def test_two_keep_scans_are_identical(tmp_path, cfg):
    z = make_keep(tmp_path)
    a, b = notes.scan(z, cfg), notes.scan(z, cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]


def test_a_note_deleted_or_trashed_in_keep_is_removed(tmp_path, cfg, conn):
    _apply(conn, notes.scan(make_keep(tmp_path / "a"), cfg))
    later = dict(KEEP)
    del later["2026-10-05T12_00_00.000-05_00.json"]                  # deleted for good
    later["Packing.json"] = {**KEEP["Packing.json"], "isTrashed": True}  # in the trash
    # Two of the four at once is half the archive: past the mass-removal bar.
    plan, result = _apply(conn, notes.scan(make_keep(tmp_path / "b", later), cfg),
                          allow_mass_removal=True)
    assert sorted(r["external_id"] for r in plan["removed"]) == [
        "keep:2026-10-05T12_00_00.000-05_00", "keep:Packing"]
    assert result["removed"] == 2
    assert _active_ids(conn) == ["keep:Deck outline", "keep:Gift ideas"]


def test_a_split_takeout_does_not_mirror_from_one_part(tmp_path, cfg, conn):
    first = make_keep(tmp_path, name="takeout-20261021T120000Z-001.zip")
    make_keep(tmp_path, {"Packing.json": KEEP["Packing.json"]},
              name="takeout-20261021T120000Z-002.zip")
    _apply(conn, notes.scan(first, cfg))
    scan = notes.scan(first, cfg)
    assert "present_ids" not in scan
    text = "\n".join(notes.report_lines(scan, obs.build_plan(conn, scan)))
    assert "not mirroring deletions: this Takeout is split" in text
    assert "takeout-20261021T120000Z-002.zip" in text


def test_keep_report_names_the_account_handles_and_trash(tmp_path, cfg, conn):
    scan = notes.scan(make_keep(tmp_path), cfg)
    text = "\n".join(notes.report_lines(scan, obs.build_plan(conn, scan)))
    assert "(Google Keep Takeout; 5 note(s) in the source)" in text
    assert "account: alex@example.com (from the one address on every shared note)" in text
    assert "note: 4 new, 0 changed" in text
    assert "handles: 2 address(es) notes are shared with" in text
    assert "skipped: 1 × note in Keep's trash" in text


def test_keep_import_keeps_doctor_green(tmp_path, cfg, conn):
    _apply(conn, notes.scan(make_keep(tmp_path), cfg))
    assert run_checks(conn, cfg)["failures"] == 0
    count = conn.execute("SELECT observation_count c FROM entity_handles "
                         "WHERE value='jake@example.com'").fetchone()["c"]
    assert count == 1


# ---------------------------------------------------------------- default target

def test_default_target_is_the_configured_path_first(tmp_path, cfg, monkeypatch):
    vault = make_vault(tmp_path / "Vault")
    cfg.connectors = {"notes": {"path": str(vault)}}
    monkeypatch.setattr(notes.platforms, "is_mac", lambda: True)
    monkeypatch.setattr(notes.platforms, "mac_notes_db", lambda: tmp_path / "NoteStore.sqlite")
    (tmp_path / "NoteStore.sqlite").write_bytes(b"x")
    assert notes.default_target(cfg) == vault


def test_a_configured_path_that_does_not_exist_says_so(tmp_path, cfg):
    cfg.connectors = {"notes": {"path": str(tmp_path / "gone")}}
    with pytest.raises(DomainError, match=r"\[connectors\.notes\] path .* does not exist"):
        notes.default_target(cfg)


def test_default_target_on_a_mac_is_the_apple_notes_store(tmp_path, cfg, monkeypatch):
    store = tmp_path / "group.com.apple.notes" / "NoteStore.sqlite"
    store.parent.mkdir()
    store.write_bytes(b"x")
    monkeypatch.setattr(notes.platforms, "is_mac", lambda: True)
    monkeypatch.setattr(notes.platforms, "mac_notes_db", lambda: store)
    monkeypatch.setattr(notes, "downloads_dir", lambda: tmp_path / "Downloads")
    assert notes.default_target(cfg) == store


def test_a_protected_store_asks_for_full_disk_access(tmp_path, cfg, monkeypatch):
    class Protected(type(tmp_path)):
        def exists(self, **kw):
            raise PermissionError(1, "Operation not permitted", str(self))
    monkeypatch.setattr(notes.platforms, "is_mac", lambda: True)
    monkeypatch.setattr(notes.platforms, "mac_notes_db",
                        lambda: Protected(tmp_path / "NoteStore.sqlite"))
    with pytest.raises(DomainError, match="Full Disk Access"):
        notes.default_target(cfg)


def test_default_target_is_the_newest_takeout_holding_keep(tmp_path, cfg, monkeypatch):
    downloads = tmp_path / "Downloads"
    old = make_keep(downloads, name="takeout-20260101T000000Z-001.zip")
    new = make_keep(downloads, name="takeout-20261001T000000Z-001.zip")
    mail = downloads / "takeout-20261015T000000Z-001.zip"
    with zipfile.ZipFile(mail, "w") as zf:
        zf.writestr("Takeout/Mail/all.mbox", "From x")
    for i, p in enumerate((old, new, mail)):
        os.utime(p, (MTIME + i * 100, MTIME + i * 100))
    monkeypatch.setattr(notes.platforms, "is_mac", lambda: False)
    monkeypatch.setattr(notes, "downloads_dir", lambda: downloads)
    assert notes.default_target(cfg) == new


def test_no_notes_source_anywhere_says_where_it_looked(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(notes.platforms, "is_mac", lambda: False)
    monkeypatch.setattr(notes, "downloads_dir", lambda: tmp_path / "Downloads")
    with pytest.raises(DomainError, match=r"no notes source found \(looked in: .*takeout-\*\.zip"):
        notes.default_target(cfg)


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch, extra_config=""):
    root = cli_project(tmp_path, monkeypatch)
    if extra_config:
        with open(root / "config.toml", "a", encoding="utf-8") as f:
            f.write(extra_config)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def _count_obs():
    r = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations "
                                   "WHERE status='active'", "--json"])
    return json.loads(r.output)[0]["c"]


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["observe", "notes", str(make_vault(tmp_path / "Vault"))])
    assert r.exit_code == 0, r.output
    assert "note: 4 new" in r.output and "dry run — nothing written" in r.output
    assert _count_obs() == 0


def test_cli_apply_then_reimport_is_a_no_op(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    z = make_keep(tmp_path / "dl")
    r = runner.invoke(app, ["observe", "notes", str(z), "--apply"])
    assert r.exit_code == 0, r.output
    assert "4 added" in r.output
    again = runner.invoke(app, ["observe", "notes", str(z), "--apply"])
    assert "0 added" in again.output and "4 identical skipped" in again.output
    assert _count_obs() == 4
    dry = runner.invoke(app, ["observe", "notes", str(z)])
    assert "already imported" in dry.output


def test_cli_reads_the_configured_path_when_given_none(tmp_path, monkeypatch):
    vault = make_vault(tmp_path / "Vault")
    _cli_setup(tmp_path, monkeypatch, f"[connectors.notes]\npath = '{vault}'\n")
    r = runner.invoke(app, ["observe", "notes", "--json"])
    assert r.exit_code == 0, r.output
    summary = json.loads(r.output)
    assert summary["connector"] == "notes" and summary["new"] == 4
    assert summary["facts"]["source"] == "obsidian"


def test_cli_removes_a_deleted_note_with_a_snapshot_first(tmp_path, monkeypatch):
    root = _cli_setup(tmp_path, monkeypatch)
    vault = make_vault(tmp_path / "Vault")
    assert runner.invoke(app, ["observe", "notes", str(vault), "--apply"]).exit_code == 0
    (vault / "Inbox.md").unlink()
    r = runner.invoke(app, ["observe", "notes", str(vault)])
    assert "remove: 1 no longer in the source (deleted or trashed)" in r.output
    assert "Weekend plans (1)" in r.output
    r = runner.invoke(app, ["observe", "notes", str(vault), "--apply"])
    assert r.exit_code == 0, r.output
    assert "1 removed" in r.output and "snapshot before removing:" in r.output
    assert list((root / "backups").glob("pre-removal-*.db"))


def test_cli_connectors_lists_notes(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0, r.output
    assert "notes" in r.output and "note " in r.output
    assert "notes" in base.registry()

"""Notes: two folders with the same name are two sources.

A folder note's id was 'file:<folder name>/<path>', but mirroring is scoped
to the folder's full path. Two folders both called Notes that both held an
Inbox.md therefore shared one id: importing the second overwrote the first's
note (its text survived only in the audit log, and every later import of the
first was a CONFLICT), and once the first's Inbox.md was deleted, its mirror
removed the row the second folder still held.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from lifecrm.connectors import notes
from lifecrm.domain import observations as obs


def _note(folder: Path, rel: str, text: str, day: int) -> None:
    p = folder / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    t = datetime(2026, 10, day, 17, 0, tzinfo=timezone.utc).timestamp()
    os.utime(p, (t, t))


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _titles(conn):
    return sorted(r["title"] for r in conn.execute(
        "SELECT title FROM observations WHERE stream='note' AND status='active'"))


def test_two_folders_called_notes_keep_their_own_inbox(tmp_path, cfg, conn):
    home, work = tmp_path / "home" / "Notes", tmp_path / "work" / "Notes"
    _note(home, "Inbox.md", "# Groceries\nmilk", day=1)
    _note(work, "Inbox.md", "# Board prep\nslides", day=5)
    _apply(conn, notes.scan(home, cfg))
    plan, result = _apply(conn, notes.scan(work, cfg))
    assert result["added"] == 1 and plan["updates"] == [] and plan["conflicts"] == []
    assert _titles(conn) == ["Board prep", "Groceries"]

    # and the first folder re-imports as itself: no conflict with the other
    again = obs.build_plan(conn, notes.scan(home, cfg))
    assert again["identical"] == 1 and again["conflicts"] == []


def test_one_folders_mirror_never_removes_the_other_folders_note(tmp_path, cfg, conn):
    home, work = tmp_path / "home" / "Notes", tmp_path / "work" / "Notes"
    _note(home, "Inbox.md", "# Groceries\nmilk", day=1)
    _note(work, "Inbox.md", "# Board prep\nslides", day=5)
    _apply(conn, notes.scan(home, cfg))
    _apply(conn, notes.scan(work, cfg))

    (home / "Inbox.md").unlink()
    _note(home, "Other.md", "# Other\nsomething", day=6)
    plan, result = _apply(conn, notes.scan(home, cfg))
    assert [r["title"] for r in plan["removed"]] == ["Groceries"]
    assert result["removed"] == 1
    assert _titles(conn) == ["Board prep", "Other"]


def test_the_folder_key_is_where_the_folder_is_inside_the_home_folder(tmp_path, monkeypatch):
    """The same synced folder under another machine's home, whatever that
    home is called, keeps its ids; a folder of the same name elsewhere does
    not share them."""
    laptop, desktop = tmp_path / "Users" / "alex", tmp_path / "Users" / "alex.rivera"
    for home in (laptop, desktop):
        (home / "OneDrive" / "Notes").mkdir(parents=True)
    (laptop / "Documents" / "Notes").mkdir(parents=True)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: laptop))
    on_laptop = notes.folder_key((laptop / "OneDrive" / "Notes").resolve())
    elsewhere = notes.folder_key((laptop / "Documents" / "Notes").resolve())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: desktop))
    on_desktop = notes.folder_key((desktop / "OneDrive" / "Notes").resolve())

    assert on_laptop == on_desktop
    assert on_laptop != elsewhere
    assert on_laptop.startswith("Notes~") and elsewhere.startswith("Notes~")


def test_a_folder_outside_the_home_folder_is_keyed_by_its_full_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nobody"))
    a = (tmp_path / "D" / "Notes")
    b = (tmp_path / "E" / "Notes")
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert notes.folder_key(a.resolve()) != notes.folder_key(b.resolve())

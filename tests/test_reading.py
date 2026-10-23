"""Reading connector: Kindle's My Clippings.txt into highlight, the Goodreads
library export into book.

What is easy to get wrong, and is checked here: the author is the LAST
parenthesised part of a title line; 'Added on' is the e-reader's clock, read
in the home timezone with the rollover and never through the machine's
locale; an extended highlight leaves its shorter self in the file (keep the
longest, count the rest); a Kindle set to another language is counted, not
guessed at; Goodreads' Excel-quoted ISBNs and <br/> reviews; and a later
Goodreads export winning over an earlier one although no row says when it
changed.
"""

import json
import os
import zipfile
from datetime import date, datetime, timezone

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.connectors import reading
from lifecrm.demo import persona
from lifecrm.demo.sources import reading as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()
ANCHOR = date(2026, 10, 21)
CONFIRMED = ("entries", "measurements", "mentions", "entity_aliases", "entity_links")
ADDED = "Wednesday, June 10, 2026 3:12:33 PM"          # 20:12:33Z (CDT)


def clip(title="The Book (An Author)", kind="Highlight", where="on page 12 | Location 180-182",
         added=ADDED, text="Some passage."):
    return f"{title}\r\n- Your {kind} {where} | Added on {added}\r\n\r\n{text}\r\n==========\r\n"


def clippings(tmp_path, *blocks, name="My Clippings.txt", bom=True):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + "".join(blocks).encode("utf-8"))
    return path


GR_HEADER = ("Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,My Rating,"
             "Average Rating,Publisher,Binding,Number of Pages,Year Published,"
             "Original Publication Year,Date Read,Date Added,Bookshelves,"
             "Bookshelves with positions,Exclusive Shelf,My Review,Spoiler,Private Notes,"
             "Read Count,Owned Copies")


def gr_row(book_id="1", title="A Book", author="An Author", rating="0", read="",
           added="2026/06/01", shelves="to-read", shelf="to-read", review="",
           isbn='"=""0123456789"""', isbn13='"=""9780123456786"""', pages="300"):
    return (f"{book_id},{title},{author},\"Author, An\",,{isbn},{isbn13},{rating},4.00,"
            f"Pub,Paperback,{pages},2020,2019,{read},{added},{shelves},,{shelf},"
            f"{review},,secret private note,1,0")


def goodreads(tmp_path, *rows, name="goodreads_library_export.csv", when=None, bom=False):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join((GR_HEADER, *rows)) + "\n"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    if when is not None:
        t = when.timestamp()
        os.utime(path, (t, t))
    return path


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _detail(rec):
    return json.loads(rec["value_json"])


def _day(offset):
    return persona.day(ANCHOR, offset).isoformat()


def _demo(tmp_path, cfg, combo="android"):
    kindle, library = sample.write_samples(tmp_path, ANCHOR, combo)
    return reading.scan(kindle, cfg), reading.scan(library, cfg)


# ---------------------------------------------------------------- the demo exports

def test_the_demo_clippings_tell_the_persona_story(tmp_path, cfg):
    scan, _ = _demo(tmp_path, cfg)
    over = [r for r in scan["records"] if r["title"] == sample.OVERSTORY]
    assert {r["journal_date"] for r in over} == {_day(-16), _day(-1)}
    assert all(_detail(r)["author"] == "Richard Powers" for r in over)
    # The highlight extended after the fact: only the longer one, counted.
    patience = [r for r in over if r["body"].startswith("Patience")]
    assert [r["body"] for r in patience] == [
        "Patience is the oldest thing in the woods, older than the soil it stands in."]
    assert _detail(patience[0])["location"] == "851-853"
    notes = {r["body"]: r["journal_date"] for r in over if _detail(r)["kind"] == "note"}
    assert notes == {"Remember for Bled: the forest above Vintgar Gorge.": _day(-16),
                     "Loved the ending.": _day(-1)}
    assert scan["skipped"] == ["1 × " + reading.SKIP_BOOKMARK,
                               "1 × " + reading.SKIP_EDITED,
                               "1 × " + reading.SKIP_LIMIT]
    assert len(scan["records"]) == 7 and scan["unmapped"] == []


def test_the_demo_goodreads_export_marks_the_overstory_read_with_five_stars(tmp_path, cfg):
    _, scan = _demo(tmp_path, cfg)
    books = {r["title"]: r for r in scan["records"]}
    over = books["The Overstory"]
    assert (over["stream"], over["journal_date"], over["value"], over["unit"]) == \
        ("book", _day(-1), 5.0, "stars")
    assert over["body"] == "Richard Powers\n\nLoved the ending.\n\nRead most of it on rest days."
    assert _detail(over)["exclusive_shelf"] == "read"
    assert _detail(over)["isbn13"] == "9780393635522" and _detail(over)["isbn"] == "039363552X"
    hail = books["Project Hail Mary"]
    assert (hail["journal_date"], hail["value"], hail["unit"]) == (_day(-16), None, None)
    assert _detail(hail)["exclusive_shelf"] == "to-read" and "date_read" not in _detail(hail)
    assert hail["external_id"] == "goodreads:54493401"


def test_both_combos_have_the_same_exports(tmp_path, cfg):
    a = _demo(tmp_path / "a", cfg, "android")
    b = _demo(tmp_path / "b", cfg, "iphone")
    assert [s["records"] for s in a] == [s["records"] for s in b]


def test_the_demo_sample_is_deterministic(tmp_path, cfg):
    a, b = _demo(tmp_path / "a", cfg), _demo(tmp_path / "b", cfg)
    assert a[0]["records"] == b[0]["records"] and a[1]["records"] == b[1]["records"]
    assert (a[0]["source_hash"], a[1]["source_hash"]) == (b[0]["source_hash"],
                                                           b[1]["source_hash"])


def test_an_unknown_combo_has_no_sample(tmp_path):
    with pytest.raises(ValueError, match="unknown combo"):
        sample.write_samples(tmp_path, ANCHOR, "palm")


@pytest.mark.parametrize("combo", ["android", "iphone"])
def test_the_demo_applies_once_then_is_identical_and_doctor_stays_green(
        tmp_path, cfg, conn, combo):
    for path in sample.write_samples(tmp_path, ANCHOR, combo):
        _, first = _apply(conn, reading.scan(path, cfg))
        assert first["added"] in (7, 4)
        plan, again = _apply(conn, reading.scan(path, cfg))
        assert again["added"] == 0 and plan["identical"] == first["added"]
    for t in CONFIRMED + ("entity_handles",):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_authors_are_not_handles(tmp_path, cfg):
    for scan in _demo(tmp_path, cfg):
        assert scan["handles"] == []
        assert all(r["participants"] == [] for r in scan["records"])


# ---------------------------------------------------------------- Kindle

@pytest.mark.parametrize("line, title, author", [
    ("The Overstory: A Novel (Richard Powers)", "The Overstory: A Novel", "Richard Powers"),
    ("The Name of the Wind (Kingkiller Chronicle, Book 1) (Patrick Rothfuss)",
     "The Name of the Wind (Kingkiller Chronicle, Book 1)", "Patrick Rothfuss"),
    ("\ufeffBraiding Sweetgrass (Kimmerer, Robin Wall)", "Braiding Sweetgrass",
     "Kimmerer, Robin Wall"),
    ("my-notes.pdf", "my-notes.pdf", None),
])
def test_the_author_is_the_last_parenthesised_part(line, title, author):
    assert reading._split_title(line) == (title, author)


@pytest.mark.parametrize("added, started", [
    ("Wednesday, June 10, 2026 3:12:33 PM", "2026-06-10T20:12:33Z"),       # US
    ("Wednesday, 10 June 2026 15:12:33", "2026-06-10T20:12:33Z"),          # UK
    ("Wednesday, June 10, 2026, 03:12 PM", "2026-06-10T20:12:00Z"),        # old Kindle
    ("Sunday, March 1, 2026 12:05:00 AM", "2026-03-01T06:05:00Z"),         # CST, midnight
])
def test_added_on_is_the_kindle_clock_in_the_home_timezone(tmp_path, cfg, added, started):
    (rec,) = reading.scan(clippings(tmp_path, clip(added=added)), cfg)["records"]
    assert rec["started_at"] == started and rec["last_modified_at"] == started


def test_a_highlight_after_midnight_counts_toward_the_evening_before(tmp_path, cfg):
    (rec,) = reading.scan(clippings(tmp_path, clip(
        added="Thursday, June 11, 2026 12:40:00 AM")), cfg)["records"]
    assert rec["journal_date"] == "2026-06-10"


def test_a_clipping_record_carries_book_text_and_place(tmp_path, cfg):
    (rec,) = reading.scan(clippings(tmp_path, clip(text="Line one.\r\nLine two.")),
                          cfg)["records"]
    assert (rec["stream"], rec["title"], rec["body"]) == ("highlight", "The Book",
                                                          "Line one.\nLine two.")
    assert _detail(rec) == {"author": "An Author", "kind": "highlight",
                            "location": "180-182", "page": "12"}
    assert rec["origin_app"] == "kindle" and rec["value"] is None
    assert rec["external_id"].startswith("kindle:")


def test_an_old_kindles_short_location_is_written_out(tmp_path, cfg):
    old = clip(where="Loc. 1180-92").replace("- Your Highlight", "- Highlight")
    (rec,) = reading.scan(clippings(tmp_path, old), cfg)["records"]
    assert _detail(rec)["location"] == "1180-1192" and "page" not in _detail(rec)


def test_an_extended_highlight_keeps_the_longest_and_counts_the_rest(tmp_path, cfg):
    path = clippings(
        tmp_path,
        clip(where="on page 5 | Location 100-101", text="Trees talk"),
        clip(where="on page 5 | Location 100-102", text="Trees talk to each other",
             added="Wednesday, June 10, 2026 3:13:00 PM"),
        clip(where="on page 5 | Location 104-105", text="underground",
             added="Wednesday, June 10, 2026 3:14:00 PM"),
        # extended over both earlier ones: one passage, the longest stands for it
        clip(where="on page 5 | Location 100-105",
             text="Trees talk to each other, underground.",
             added="Wednesday, June 10, 2026 3:15:00 PM"),
        clip(where="on page 9 | Location 300-301", text="Trees talk",
             added="Wednesday, June 10, 2026 3:16:00 PM"))   # same words, elsewhere
    scan = reading.scan(path, cfg)
    assert sorted(r["body"] for r in scan["records"]) == [
        "Trees talk", "Trees talk to each other, underground."]
    assert scan["skipped"] == ["3 × " + reading.SKIP_EDITED]


def test_a_clipping_listed_twice_is_one_record(tmp_path, cfg):
    scan = reading.scan(clippings(tmp_path, clip(), clip()), cfg)
    assert len(scan["records"]) == 1
    assert scan["skipped"] == ["1 × " + reading.SKIP_TWICE]


def test_an_edited_note_at_the_same_place_is_one_record_the_latest_kept(tmp_path, cfg):
    scan = reading.scan(clippings(
        tmp_path,
        clip(kind="Note", where="on page 12 | Location 182", text="Good line"),
        clip(kind="Note", where="on page 12 | Location 182", text="Reread before Bled",
             added="Thursday, June 11, 2026 9:00:00 AM"),
        clip(kind="Highlight", where="on page 12 | Location 180-182", text="A passage")),
        cfg)
    notes = [r for r in scan["records"] if _detail(r)["kind"] == "note"]
    assert [r["body"] for r in notes] == ["Reread before Bled"]
    assert len(scan["records"]) == 2                    # the highlight is its own record
    assert scan["skipped"] == ["1 × " + reading.SKIP_REPLACED]


def test_extending_a_highlight_forward_updates_the_stored_one(tmp_path, cfg, conn):
    _apply(conn, reading.scan(clippings(tmp_path / "a", clip(text="Trees talk")), cfg))
    longer = clippings(tmp_path / "b", clip(text="Trees talk"), clip(
        where="on page 12 | Location 180-185", text="Trees talk to each other.",
        added="Thursday, June 11, 2026 9:00:00 AM"))
    plan, result = _apply(conn, reading.scan(longer, cfg))
    assert (result["added"], result["updated"]) == (0, 1)
    row = conn.execute("SELECT body, journal_date FROM observations").fetchone()
    assert row["body"] == "Trees talk to each other." and row["journal_date"] == "2026-06-11"


def test_bookmarks_limits_and_empty_clippings_are_skipped_and_counted(tmp_path, cfg):
    scan = reading.scan(clippings(
        tmp_path,
        clip(kind="Bookmark", where="on page 3 | Location 40", text=""),
        clip(text="<You have reached the clipping limit for this item>"),
        clip(text=""),
        clip(text="Kept.")), cfg)
    assert [r["body"] for r in scan["records"]] == ["Kept."]
    assert scan["skipped"] == ["1 × " + reading.SKIP_BOOKMARK,
                               "1 × " + reading.SKIP_EMPTY,
                               "1 × " + reading.SKIP_LIMIT]


def test_clippings_in_another_language_are_counted_not_guessed(tmp_path, cfg):
    german = ("Der Report (Autorin)\r\n- Ihre Markierung bei Position 180-182 | "
              "Hinzugefügt am Mittwoch, 10. Juni 2026 15:12:33\r\n\r\nText\r\n==========\r\n")
    scan = reading.scan(clippings(tmp_path, german, german, clip(text="English one.")), cfg)
    assert [r["body"] for r in scan["records"]] == ["English one."]
    (u,) = scan["unmapped"]
    assert (u["what"], u["count"]) == (reading.NOT_ENGLISH, 2)
    # The details line, cut short — never the highlighted text.
    assert u["example"].startswith("My Clippings.txt: - Ihre Markierung bei Position 180-182")
    assert "Text" not in u["example"]
    lines = reading.report_lines(scan, {"new": [], "updates": [], "identical": 0,
                                        "refreshed": [], "dup_incoming": 0, "conflicts": [],
                                        "invalid": []})
    assert any(line.startswith("  not understood: 2 × clipping whose details line is not "
                               "in English") for line in lines)


def test_an_unreadable_added_on_is_counted(tmp_path, cfg):
    scan = reading.scan(clippings(tmp_path, clip(added="sometime last week"),
                                  clip(text="Fine.")), cfg)
    assert [r["body"] for r in scan["records"]] == ["Fine."]
    assert scan["unmapped"][0]["what"] == "clipping with an unreadable 'Added on' time"


def test_month_names_are_read_by_the_connector_itself(tmp_path, cfg):
    """English month names and abbreviations come from the connector's own
    table, so a machine running in another locale reads them the same."""
    (rec,) = reading.scan(clippings(tmp_path, clip(added="Wed, Sept 9, 2026 9:05:00 am")),
                          cfg)["records"]
    assert rec["started_at"] == "2026-09-09T14:05:00Z"


# ---------------------------------------------------------------- Goodreads

def test_a_goodreads_row_maps_to_a_book(tmp_path, cfg):
    path = goodreads(tmp_path, gr_row(
        book_id="77", title="Read It", rating="4", read="2026/06/10", added="2026/05/01",
        shelves='"favorites, sci-fi"', shelf="read",
        review='"Great.<br/><br/>Would read again &amp; again."'))
    (rec,) = reading.scan(path, cfg)["records"]
    assert (rec["external_id"], rec["title"], rec["value"], rec["unit"]) == \
        ("goodreads:77", "Read It", 4.0, "stars")
    assert rec["journal_date"] == "2026-06-10" and rec["started_at"] == "2026-06-10T17:00:00Z"
    assert rec["body"] == "An Author\n\nGreat.\n\nWould read again & again."
    assert _detail(rec) == {
        "author": "An Author", "isbn": "0123456789", "isbn13": "9780123456786",
        "shelves": ["favorites", "sci-fi"], "exclusive_shelf": "read",
        "date_read": "2026-06-10", "date_added": "2026-05-01", "pages": 300,
        "read_count": 1, "year": 2019}
    assert "secret private note" not in json.dumps(rec)
    assert rec["origin_app"] == "goodreads" and rec["participants"] == []


def test_an_unread_book_is_dated_by_when_it_was_added_and_has_no_rating(tmp_path, cfg):
    (rec,) = reading.scan(goodreads(tmp_path, gr_row(isbn='"="""', isbn13='"="""')),
                          cfg)["records"]
    assert rec["journal_date"] == "2026-06-01" and rec["value"] is None
    assert "isbn" not in _detail(rec) and "isbn13" not in _detail(rec)
    assert rec["body"] == "An Author"


def test_rows_without_an_id_or_any_date_are_counted(tmp_path, cfg):
    scan = reading.scan(goodreads(tmp_path, gr_row(book_id=""), gr_row(added=""),
                                  gr_row(book_id="2", added="June-ish"),
                                  gr_row(book_id="3")), cfg)
    assert [r["external_id"] for r in scan["records"]] == ["goodreads:3"]
    assert {u["what"]: u["count"] for u in scan["unmapped"]} == {
        "Goodreads row with no Book Id or Title": 1,
        "Goodreads row with neither Date Read nor Date Added": 2,
        "Goodreads row with an unreadable Date Added": 1}


def test_a_later_export_updates_a_book_and_an_older_one_cannot(tmp_path, cfg, conn):
    t1 = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 2, 12, tzinfo=timezone.utc)
    old = goodreads(tmp_path / "old", gr_row(), when=t1)
    new = goodreads(tmp_path / "new", gr_row(rating="5", read="2026/06/20", shelf="read"),
                    when=t2)
    _apply(conn, reading.scan(old, cfg))
    assert conn.execute("SELECT last_modified_at FROM observations").fetchone()[0] == \
        "2026-06-02T12:00:00Z"
    plan, result = _apply(conn, reading.scan(new, cfg))
    assert result["updated"] == 1
    row = conn.execute("SELECT value, journal_date FROM observations").fetchone()
    assert (row["value"], row["journal_date"]) == (5.0, "2026-06-20")
    again = reading.scan(old, cfg)
    plan = obs.build_plan(conn, again)
    assert len(plan["conflicts"]) == 1 and not plan["updates"]
    assert any(line.startswith("  kept existing: 1 book(s) differ from what a later export")
               for line in reading.report_lines(again, plan))


def test_the_export_is_never_older_than_its_newest_date(tmp_path, cfg):
    stale = datetime(1980, 1, 1, tzinfo=timezone.utc)
    (rec,) = reading.scan(goodreads(tmp_path, gr_row(read="2026/06/20", shelf="read"),
                                    when=stale), cfg)["records"]
    assert rec["last_modified_at"] == "2026-06-20T17:00:00Z"


def test_a_bom_prefixed_goodreads_export_reads(tmp_path, cfg):
    assert len(reading.scan(goodreads(tmp_path, gr_row(), bom=True), cfg)["records"]) == 1


# ---------------------------------------------------------------- targets & bad input

def test_a_folder_or_zip_holding_both_is_one_scan(tmp_path, cfg):
    folder = tmp_path / "exports"
    clippings(folder / "Kindle" / "documents", clip())
    goodreads(folder, gr_row())
    (folder / "Kindle" / "documents" / "book.azw3").write_bytes(b"\0")
    scan = reading.scan(folder, cfg)
    assert sorted({r["stream"] for r in scan["records"]}) == ["book", "highlight"]
    assert scan["unread"] == ["Kindle/documents/book.azw3"]
    zipped = tmp_path / "reading.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(folder).as_posix())
    assert [r["external_id"] for r in reading.scan(zipped, cfg)["records"]] == \
        [r["external_id"] for r in scan["records"]]


@pytest.mark.parametrize("name, content, expected", [
    ("library.csv", "Title,Author,Shelf\nX,Y,Z\n",
     "is neither a Kindle 'My Clippings.txt' nor a Goodreads library export — it begins: "
     "Title,Author,Shelf"),
    ("notes.txt", "just some notes\n", "it begins: just some notes"),
    ("empty.txt", "", "it begins: (empty)"),
])
def test_a_file_that_is_neither_fails_in_one_line(tmp_path, cfg, name, content, expected):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    with pytest.raises(DomainError) as e:
        reading.scan(path, cfg)
    assert expected in str(e.value) and "\n" not in str(e.value)


def test_a_folder_holding_neither_names_what_it_has(tmp_path, cfg):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "photo.jpg").write_bytes(b"\xff\xd8")
    with pytest.raises(DomainError, match=r"holds no Kindle 'My Clippings.txt' and no "
                                          r"Goodreads library export — it has: photo.jpg"):
        reading.scan(tmp_path / "x", cfg)


def test_a_clippings_file_that_is_not_utf8_fails_in_one_line(tmp_path, cfg):
    path = tmp_path / "My Clippings.txt"
    path.write_bytes(clip().encode("utf-8") + b"\xff\xfe broken")
    with pytest.raises(DomainError, match="is not valid UTF-8"):
        reading.scan(path, cfg)


def test_a_goodreads_header_without_author_is_refused(tmp_path, cfg):
    path = tmp_path / "goodreads_library_export.csv"
    path.write_text("Book Id,Title,My Rating\n1,X,0\n", encoding="utf-8")
    with pytest.raises(DomainError, match="looks like a Goodreads export but has no author"):
        reading.scan(path, cfg)


# ---------------------------------------------------------------- determinism, report, mirroring

def test_scan_is_deterministic(tmp_path, cfg):
    sample.write_samples(tmp_path, ANCHOR, "android")
    a, b = reading.scan(tmp_path, cfg), reading.scan(tmp_path, cfg)
    assert a == b and a["source_hash"]


def test_report_names_each_stream_with_its_span(tmp_path, cfg, conn):
    sample.write_samples(tmp_path / "x", ANCHOR, "android")
    scan = reading.scan(tmp_path / "x", cfg)
    lines = reading.report_lines(scan, obs.build_plan(conn, scan))
    text = "\n".join(lines)
    martian = persona.day(ANCHOR, sample.MARTIAN_OFFSET - 2).isoformat()
    assert f"  highlight: 7 new, 0 changed | {martian}..{_day(-1)} | e.g. " in text
    added = persona.day(ANCHOR, sample.MARTIAN_OFFSET).isoformat()
    assert f"  book: 4 new, 0 changed | {added}..{_day(-1)} | e.g. {_day(-1)} " \
           f"The Overstory (read, 5 star(s))" in text
    assert "  shelves: read 2, to-read 2 | rated 2" in text
    assert "  most highlighted: The Overstory: A Novel (6), The Martian (1)" in text
    assert "skipped: 1 × bookmark" in text


def test_nothing_is_mirrored(tmp_path, cfg, conn):
    _apply(conn, reading.scan(clippings(tmp_path / "a", clip(text="One."),
                                        clip(text="Two.", where="on page 40 | Location 600")),
                              cfg))
    scan = reading.scan(clippings(tmp_path / "b", clip(text="One.")), cfg)
    assert "present_ids" not in scan
    plan, _ = _apply(conn, scan)
    assert plan["removed"] == []
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


# ---------------------------------------------------------------- default target & CLI

def test_default_target_is_the_configured_path(tmp_path, cfg):
    path = clippings(tmp_path, clip())
    cfg.connectors = {"reading": {"path": str(path)}}
    assert reading.default_target(cfg) == path
    cfg.connectors = {"reading": {"path": str(tmp_path / "missing.txt")}}
    with pytest.raises(DomainError, match=r"\[connectors.reading\] path does not exist"):
        reading.default_target(cfg)


def test_default_target_finds_a_plugged_in_kindle_or_a_download(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    kindle = clippings(tmp_path / "Kindle" / "documents", clip())
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])
    with pytest.raises(DomainError, match="no reading export found — no Kindle is plugged in"):
        reading.default_target(cfg)
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [kindle])
    assert reading.default_target(cfg) == kindle
    csv_path = goodreads(tmp_path / "Downloads", gr_row())
    with pytest.raises(DomainError, match="found several reading exports"):
        reading.default_target(cfg)
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])
    assert reading.default_target(cfg) == csv_path


def test_cli_dry_run_then_apply_then_identical(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    kindle, library = sample.write_samples(tmp_path / "exp", ANCHOR, "iphone")
    dry = runner.invoke(app, ["observe", "reading", str(kindle)])
    assert dry.exit_code == 0, dry.output
    assert "highlight: 7 new" in dry.output and "dry run — nothing written" in dry.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0
    for path, n in ((kindle, 7), (library, 4)):
        applied = runner.invoke(app, ["observe", "reading", str(path), "--apply"])
        assert applied.exit_code == 0, applied.output
        assert f"{n} added" in applied.output
        again = runner.invoke(app, ["observe", "reading", str(path), "--apply"])
        assert "0 added" in again.output and f"{n} identical skipped" in again.output
    streams = runner.invoke(app, ["sql", "SELECT stream, COUNT(*) c FROM observations "
                                         "GROUP BY stream ORDER BY stream", "--json"])
    assert json.loads(streams.output) == [{"stream": "book", "c": 4},
                                          {"stream": "highlight", "c": 7}]

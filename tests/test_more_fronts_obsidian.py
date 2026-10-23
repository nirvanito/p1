"""The Obsidian vault's day digest itemizes the newer sources — emails with
the people they passed between, workouts with their summary, rides and food
orders, highlights and books by title — and counts listening and searches.
Imported text stays inert Markdown, and a Cc is never an encounter."""

from lifecrm.export import build_vault
from test_more_fronts_review import _apply, _rec
from test_more_fronts_web import seed_more

TITLE = "The *Overstory* [x](http://e.x) #tag"


def _vault(conn):
    seed_more(conn)
    _apply(conn, "reading", None, [
        _rec(80, "highlight", "2026-07-27", "21:00", title=TITLE,
             body="A tree is a ![](http://e.x/img.png) story", origin_app="kindle"),
        _rec(81, "highlight", "2026-07-27", "21:05", title=TITLE, body="second",
             origin_app="kindle"),
        _rec(82, "book", "2026-07-27", "12:00", title=TITLE, body="Richard Powers",
             value=5.0, unit="stars", origin_app="goodreads"),
    ])
    _apply(conn, "fitness-2", None, [
        _rec(83, "workout", "2026-07-30", "06:30", title="Leg Day", value=65.0,
             unit="min", origin_app="strong",
             body="Squat (Barbell) 3×5 @ 100 kg\nfelt *strong*"),
    ])
    conn.commit()
    return build_vault(conn, tz_name="America/Chicago")


def _day(notes, jd):
    return notes[f"Journal/{jd[:4]}/{jd}.md"]


def test_emails_are_itemized_with_the_people_they_passed_between(conn):
    notes = _vault(conn)
    day = _day(notes, "2026-07-28")
    assert "- 1 email with [[Jake Morales]] · latest " in day
    assert "“Bled photos”" in day
    assert "1 text with [[Jake Morales]] (instagram_dm)" in day
    # Rosa was only copied on Jake's 07-25 mail
    jul25 = _day(notes, "2026-07-25")
    assert "email with [[Jake Morales]]" in jul25 and "[[Rosa Treviño]]" not in jul25
    rosa = notes["People/Rosa Treviño.md"]
    assert "last_seen: 2026-06-20" in rosa


def test_workouts_rides_orders_listening_and_searches(conn):
    notes = _vault(conn)
    legs = _day(notes, "2026-07-30")
    assert "workout: Leg Day (65 min) — Squat (Barbell) 3×5 @ 100 kg — observed" in legs
    assert "felt" not in legs                     # the summary's first line only
    sunday = _day(notes, "2026-07-26")
    assert "ride: Uber to Uchi · 16.87 USD — observed `" in sunday
    assert "food order: Chipotle · 18.95 USD" in sunday
    assert "- listened 10 min (1 track or episode) — observed" in sunday
    assert "- 2 searches — observed" in sunday
    assert "omakase" not in sunday               # a search is counted, not quoted


def test_books_and_highlights_by_title_stay_inert_markdown(conn):
    notes = _vault(conn)
    day = _day(notes, "2026-07-27")
    escaped = r"The \*Overstory\* \[x\](http://e.x) \#tag"
    assert f"- 2 highlights in *{escaped}* — observed" in day
    assert f"- book: *{escaped}* · rated 5 stars — observed" in day
    assert "](http" not in day.replace(r"\](http", "")
    assert "![" not in day


def test_the_vault_is_deterministic(conn):
    notes = _vault(conn)
    assert build_vault(conn, tz_name="America/Chicago") == notes

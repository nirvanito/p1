"""Sample reading exports: the persona's Kindle clippings and Goodreads
library, in each one's own format, as the `reading` connector reads them.

  Kindle    the e-reader, plugged in by USB, shows up as a drive (Windows:
            a drive letter; Mac: /Volumes/Kindle) holding
            documents/My Clippings.txt — UTF-8 with a byte-order mark, CRLF
            line endings, every clipping appended after a '==========' line.
            Kindle apps on a phone do not write this file; the e-reader does,
            whichever computer it is plugged into, so both combos get the
            same file.
  Goodreads My Books > Import and export > Export Library:
            goodreads_library_export.csv, ISBNs written as Excel formulas
            ('="0393635524"'), dates as YYYY/MM/DD, the review's line breaks
            as <br/>. A website export — the same on both combos.

The story: The Overstory, read on the rest day (-16: two chapters, a few
highlights, one extended after the fact, a note tying it to Bled, a
bookmark) and finished on -1 ("loved the ending" — the note and the
review say what the journal says). Goodreads marks it read on -1 with five
stars; Project Hail Mary and Braiding Sweetgrass, the book list's "next" and
"someday", went onto to-read on -16. The Martian, read a year and a bit ago,
explains why Project Hail Mary is next.

The highlighted passages are written for this demo — not quotations from the
books. Deterministic for (anchor, combo).
"""

from __future__ import annotations

import csv
import io
import os
from datetime import date
from pathlib import Path

from .. import persona

CLIPPINGS = "My Clippings.txt"
GOODREADS = "goodreads_library_export.csv"
OVERSTORY = "The Overstory: A Novel"
MARTIAN_OFFSET = -400

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")

# (offset, local 'HH:MM:SS', book, author, kind, page, location, text)
CLIPS: tuple[tuple[int, str, str, str, str, str, str, str], ...] = (
    (MARTIAN_OFFSET - 2, "22:04:51", "The Martian", "Andy Weir", "Highlight", "141",
     "2104-2105", "Solve one problem, then the next one, and keep going until you get home."),
    (MARTIAN_OFFSET - 2, "22:30:18", "The Martian", "Andy Weir", "Highlight", "203",
     "3011-3013", "<You have reached the clipping limit for this item>"),
    (-16, "10:41:07", OVERSTORY, "Richard Powers", "Highlight", "41", "612-614",
     "A forest is not a collection of trees. It is one long conversation, held "
     "underground in roots and threads of fungus."),
    (-16, "10:58:22", OVERSTORY, "Richard Powers", "Highlight", "57", "851-852",
     "Patience is the oldest thing in the woods"),
    (-16, "10:58:49", OVERSTORY, "Richard Powers", "Highlight", "57", "851-853",
     "Patience is the oldest thing in the woods, older than the soil it stands in."),
    (-16, "11:20:15", OVERSTORY, "Richard Powers", "Note", "63", "940",
     "Remember for Bled: the forest above Vintgar Gorge."),
    (-16, "11:47:30", OVERSTORY, "Richard Powers", "Bookmark", "70", "1043", ""),
    (-1, "15:32:10", OVERSTORY, "Richard Powers", "Highlight", "470", "7102-7104",
     "We see the trunk and miss the centuries of weather written inside it."),
    (-1, "16:48:55", OVERSTORY, "Richard Powers", "Highlight", "498", "7530-7532",
     "Every ending here is only a seed, waiting for its season."),
    (-1, "16:51:03", OVERSTORY, "Richard Powers", "Note", "498", "7532",
     "Loved the ending."),
)

HEADER = ("Book Id", "Title", "Author", "Author l-f", "Additional Authors", "ISBN",
          "ISBN13", "My Rating", "Average Rating", "Publisher", "Binding",
          "Number of Pages", "Year Published", "Original Publication Year",
          "Date Read", "Date Added", "Bookshelves", "Bookshelves with positions",
          "Exclusive Shelf", "My Review", "Spoiler", "Private Notes", "Read Count",
          "Owned Copies")

# (book id, title, author, author l-f, isbn, isbn13, rating, average, publisher,
#  binding, pages, year, original year, read offset, added offset, shelves,
#  exclusive shelf, review, read count)
BOOKS = (
    ("40180098", "The Overstory", "Richard Powers", "Powers, Richard", "039363552X",
     "9780393635522", 5, "4.09", "W. W. Norton & Company", "Hardcover", 502, 2018, 2018,
     -1, -60, ("favorites",), "read",
     "Loved the ending.<br/><br/>Read most of it on rest days.", 1),
    ("54493401", "Project Hail Mary", "Andy Weir", "Weir, Andy", "0593135202",
     "9780593135204", 0, "4.52", "Ballantine Books", "Hardcover", 476, 2021, 2021,
     None, -16, ("to-read",), "to-read", "", 0),
    ("17465709", "Braiding Sweetgrass", "Robin Wall Kimmerer", "Kimmerer, Robin Wall",
     "1571313567", "9781571313560", 0, "4.52", "Milkweed Editions", "Paperback", 408,
     2013, 2013, None, -16, ("to-read",), "to-read", "", 0),
    ("18007564", "The Martian", "Andy Weir", "Weir, Andy", "0553418025",
     "9780553418026", 4, "4.42", "Crown", "Paperback", 387, 2014, 2011,
     MARTIAN_OFFSET, MARTIAN_OFFSET - 30, (), "read",
     "Funny and tense. Made me want to read Project Hail Mary next.", 1),
)


def _check(combo: str) -> None:
    if combo not in persona.COMBOS:
        raise ValueError(f"unknown combo '{combo}' (expected one of "
                         f"{', '.join(persona.COMBOS)})")


def _added_on(anchor: date, offset: int, hhmm: str) -> str:
    """Kindle's US English stamp: 'Tuesday, October 6, 2026 10:12:33 PM',
    spelled here rather than through the machine's locale."""
    dt = persona.local_dt(anchor, offset, hhmm)
    hour = dt.hour % 12 or 12
    return (f"{_DAYS[dt.weekday()]}, {_MONTHS[dt.month - 1]} {dt.day}, {dt.year} "
            f"{hour}:{dt.minute:02d}:{dt.second:02d} {'PM' if dt.hour >= 12 else 'AM'}")


def clippings_text(anchor: date) -> str:
    out = []
    for n, (off, hhmm, book, author, kind, page, loc, text) in enumerate(CLIPS):
        # Kindle starts the file with a byte-order mark, and some firmware
        # repeats it on later titles; the reader strips both.
        title = ("\ufeff" if n in (0, 5) else "") + f"{book} ({author})"
        out += [title, f"- Your {kind} on page {page} | Location {loc} | Added on "
                       f"{_added_on(anchor, off, hhmm)}",
                "", text, "=========="]
    return "\r\n".join(out) + "\r\n"


def _shelf_positions(shelves: tuple[str, ...], n: int) -> str:
    return ", ".join(f"{s} (#{n})" for s in shelves)


def _gr_date(anchor: date, offset: int | None) -> str:
    return "" if offset is None else persona.day(anchor, offset).strftime("%Y/%m/%d")


def goodreads_text(anchor: date) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    for n, (book_id, title, author, lf, isbn, isbn13, rating, avg, publisher, binding,
            pages, year, original, read, added, shelves, exclusive, review,
            read_count) in enumerate(BOOKS, start=1):
        w.writerow((book_id, title, author, lf, "", f'="{isbn}"', f'="{isbn13}"',
                    rating, avg, publisher, binding, pages, year, original,
                    _gr_date(anchor, read), _gr_date(anchor, added), ", ".join(shelves),
                    _shelf_positions(shelves, n), exclusive, review, "", "", read_count,
                    0))
    return buf.getvalue()


def _stamp(path: Path, anchor: date) -> None:
    """The file time the export would have: downloaded the morning of the
    anchor. The connector dates the Goodreads export by it."""
    t = persona.local_dt(anchor, 0, "09:00").timestamp()
    os.utime(path, (t, t))


def write_samples(dest: Path, anchor: date, combo: str) -> list[Path]:
    """The Kindle's clippings, then the Goodreads export: two sources, two
    imports (`lifecrm observe reading <path>` each)."""
    _check(combo)
    dest = Path(dest)
    kindle = dest / "Kindle" / "documents" / CLIPPINGS
    kindle.parent.mkdir(parents=True, exist_ok=True)
    kindle.write_bytes(clippings_text(anchor).encode("utf-8"))
    _stamp(kindle, anchor)
    library = dest / GOODREADS
    library.write_bytes(goodreads_text(anchor).encode("utf-8"))
    _stamp(library, anchor)
    return [kindle, library]

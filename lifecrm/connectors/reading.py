"""Reading connector: Kindle highlights and the Goodreads library.

What you underline is what stayed with you, and what you shelve is what you
meant to get to. Both already sit in plain files: the Kindle e-reader keeps
every highlight and note in `documents/My Clippings.txt`, and Goodreads
exports the whole library as one CSV. This connector reads those files. It
never logs in anywhere and never sends anything.

Accepted targets: one of the files, a folder (searched recursively — a
Kindle's own drive works, only its .txt and .csv files are opened), or a .zip.
Each file is sniffed by its content, not its name.

Kindle: My Clippings.txt -> stream `highlight`
-----------------------------------------------
Clippings are appended, never rewritten, separated by a '==========' line:

    The Overstory: A Novel (Richard Powers)
    - Your Highlight on page 12 | Location 180-182 | Added on Tuesday, October 6, 2026 10:12:33 PM

    the highlighted text
    ==========

  title        the book, as Kindle names it ('Title (Author)' — the author is
               the last parenthesised part)
  body         the highlight's or note's text
  started_at   'Added on', the e-reader's clock. It carries no zone, so the
               home timezone reads it (the configured rollover applied): a
               highlight made abroad lands on the home-zone day
  value_json   {author, kind: 'highlight' | 'note', location ('180-182'; an
               old Kindle's '180-82' written out), page}
  external_id  'kindle:' + sha1(book, author, kind, where it starts — the
               location, else the page, else the text)

The file never forgets: extend a highlight and Kindle appends the longer one
beside the original. Within a book and kind, a clipping whose place overlaps
another's and whose text contains it is the same highlight edited — the
longest is kept, the others counted. A highlight extended forward keeps its
start and so its id: re-imported, the stored one is updated to the longer
text (newer 'Added on'). Two clippings at one start whose texts differ (an
edited note) are one record, the latest kept.

Skipped and counted: bookmarks (a place, no text), the placeholder Kindle
writes when a publisher's clipping limit is reached, and empty clippings.
Only English details lines ('- Your Highlight ...', '- Highlight Loc. ...')
are read; a Kindle set to another language writes them in that language,
and those clippings are counted as not understood, with an example — never
guessed at.

Kindle apps on a phone or computer do not write My Clippings.txt; their
highlights live in the Kindle notebook on Amazon's site and are not read.

Goodreads: goodreads_library_export.csv -> stream `book`
---------------------------------------------------------
One record per book, keyed by Goodreads' own 'Book Id' ('goodreads:<id>'):

  title        Title
  body         Author, then the review (My Review; <br/> becomes a line break)
  value / unit My Rating, 'stars' (0 — not rated — is no value)
  started_at   Date Read at local noon, else Date Added: statements of dates,
               not instants, placed where every rollover keeps the date
  value_json   {author, additional_authors, isbn, isbn13 (the '="…"' Excel
               quoting removed), shelves (Bookshelves), exclusive_shelf
               (read | currently-reading | to-read), date_read, date_added,
               pages, read_count, year}

Not imported: Average Rating (the community's, not yours), Private Notes,
Spoiler, Owned Copies, and the purchase columns of older exports.

Re-importing, and what wins
---------------------------
A highlight's last_modified_at is its 'Added on'. The Goodreads export says
nothing about when a row last changed, so every book's last_modified_at is
the export's own "as of" — the file's modification time, or its newest date
when that is later. A later export therefore wins: a book moved from
to-read to read, a new rating or review updates the stored record, audited.
An older export overwrites nothing; differences show as conflicts.

Mirroring: none, for both. My Clippings.txt holds only what this e-reader
saw (a new Kindle starts an empty file; deleting a highlight on the device
never touches it), and a book removed from a Goodreads shelf is a change of
mind, not a book that was never read — the archive remembers.

Handles: none. Authors are not people you know; a handle is for someone the
owner might link to an entity, and an author's name is only text.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import string
import zipfile
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "reading"
streams = ("highlight", "book")
# No stale_after_days: both are exported by hand, when there is something new.

_ISO = "%Y-%m-%dT%H:%M:%SZ"
_SEPARATOR = re.compile(r"^\s*={10}\s*$", re.M)
_SNIFF_BYTES = 1 << 16
CLIPPINGS = "My Clippings.txt"
GOODREADS = "goodreads_library_export.csv"

_MONTHS = {m.lower(): i for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9

_META = re.compile(r"^-\s*(?:Your\s+)?(Highlight|Note|Bookmark|Clip|Article Clip)\b(.*)$",
                   re.I)
_ADDED = re.compile(r"\|\s*Added on\s+(.+?)\s*$", re.I)
_LOCATION = re.compile(r"\b(?:Location|Loc\.)\s+(\d[\d,]*)(?:\s*-\s*(\d[\d,]*))?", re.I)
_PAGE = re.compile(r"\bpage\s+([0-9ivxlcdmIVXLCDM]+(?:\s*-\s*[0-9ivxlcdmIVXLCDM]+)?)",
                   re.I)
_WHEN = re.compile(
    r"^(?:[A-Za-z]+,?\s+)?"
    r"(?:(?P<mon>[A-Za-z]+)\.?\s+(?P<day>\d{1,2})|(?P<day2>\d{1,2})\s+(?P<mon2>[A-Za-z]+)\.?)"
    r",?\s+(?P<year>\d{4}),?\s+(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?"
    r"\s*(?P<ampm>[AaPp]\.?\s?[Mm]\.?)?$")
_LIMIT = re.compile(r"^<\s*You have reached the clipping limit", re.I)
_EDGE = " \t\n.,;:!?\"'“”‘’()[]-–—…"

SKIP_BOOKMARK = "bookmark (a place in a book, no text)"
SKIP_LIMIT = ("highlight the publisher's clipping limit left out (Kindle wrote a "
              "placeholder, not the text)")
SKIP_EMPTY = "clipping with no text (an image, or nothing selected)"
SKIP_EDITED = ("earlier, shorter version of a highlight or note that was extended "
               "later (the longest is kept)")
SKIP_TWICE = "clipping listed twice (identical)"
SKIP_REPLACED = "note or highlight replaced at the same place (the latest is kept)"
NOT_ENGLISH = ("clipping whose details line is not in English (only English Kindle "
               "clippings are read — set the Kindle to English to import them)")


# ---------------------------------------------------------------- Kindle

def _collapse(text: str | None) -> str:
    return " ".join((text or "").split())


def _split_title(line: str) -> tuple[str, str | None]:
    """('The Overstory: A Novel', 'Richard Powers') from Kindle's title line.
    The author is the LAST parenthesised part: a series name before it
    ('(Kingkiller Chronicle, Book 1)') stays in the title."""
    s = _collapse(line.replace("\ufeff", ""))
    m = re.match(r"^(.*?)\s*\(([^()]*)\)$", s)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip() or None
    return s, None


def _location(m: re.Match | None) -> tuple[int, int, str] | None:
    """(start, end, '180-182'). An old Kindle shortens the end ('180-82')."""
    if m is None:
        return None
    start = int(m.group(1).replace(",", ""))
    if not m.group(2):
        return start, start, str(start)
    end_text = m.group(2).replace(",", "")
    s_text = str(start)
    if len(end_text) < len(s_text):
        end_text = s_text[:len(s_text) - len(end_text)] + end_text
    end = int(end_text)
    if end < start:
        end = start
    return start, end, f"{start}-{end}" if end != start else str(start)


def _added(text: str) -> datetime | None:
    """'Tuesday, October 6, 2026 10:12:33 PM' (US), 'Tuesday, 6 October 2026
    22:12:33' (UK) or the older 'Tuesday, October 6, 2012, 10:12 PM' — month
    names read here, never through the machine's locale."""
    m = _WHEN.match(text.strip())
    if not m:
        return None
    month = _MONTHS.get((m.group("mon") or m.group("mon2") or "").lower())
    if month is None:
        return None
    hour, minute = int(m.group("h")), int(m.group("m"))
    second = int(m.group("s") or 0)
    ampm = (m.group("ampm") or "").replace(".", "").replace(" ", "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    try:
        return datetime(int(m.group("year")), month, int(m.group("day") or m.group("day2")),
                        hour, minute, second)
    except ValueError:
        return None


class _Clip:
    __slots__ = ("book", "author", "kind", "loc", "page", "text", "added", "order")

    def __init__(self, book, author, kind, loc, page, text, added, order):
        self.book, self.author, self.kind = book, author, kind
        self.loc, self.page, self.text = loc, page, text
        self.added, self.order = added, order

    def position(self) -> str:
        if self.loc:
            return f"loc:{self.loc[0]}"
        if self.page:
            return f"page:{self.page}"
        return "text:" + hashlib.sha1(_collapse(self.text).encode("utf-8")).hexdigest()

    def external_id(self) -> str:
        key = f"{self.book}\n{self.author or ''}\n{self.kind}\n{self.position()}"
        return "kindle:" + hashlib.sha1(key.encode("utf-8")).hexdigest()


def _read_clippings(rel: str, text: str, cfg: Config, acc: "_Acc") -> None:
    found = books = 0
    seen_books: set[tuple] = set()
    blocks = _SEPARATOR.split(text)
    for block in blocks:
        lines = [ln.rstrip() for ln in block.replace("\ufeff", "").splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        if len(lines) < 2:
            acc.unknown("clipping block without a title and a details line", rel)
            continue
        found += 1
        title, author = _split_title(lines[0])
        meta = lines[1].strip()
        m = _META.match(meta)
        if not m:
            acc.unknown(NOT_ENGLISH, f"{rel}: {meta[:70]}")
            continue
        kind = m.group(1).lower()
        if kind == "bookmark":
            acc.skipped[SKIP_BOOKMARK] += 1
            continue
        if kind not in ("highlight", "note"):
            acc.unknown(f"clipping of another kind ('{m.group(1)}')", f"{rel}: {meta[:70]}")
            continue
        when = _ADDED.search(meta)
        added = _added(when.group(1)) if when else None
        if added is None:
            acc.unknown("clipping with an unreadable 'Added on' time", f"{rel}: {meta[:70]}")
            continue
        body = "\n".join(lines[2:]).strip()
        if not body:
            acc.skipped[SKIP_EMPTY] += 1
            continue
        if _LIMIT.match(body):
            acc.skipped[SKIP_LIMIT] += 1
            continue
        rest = meta[m.start(2):max(when.start(), m.start(2))]   # where, not when
        page = _PAGE.search(rest)
        local = added.replace(tzinfo=ZoneInfo(cfg.timezone))
        if (title, author) not in seen_books:
            seen_books.add((title, author))
            books += 1
        acc.clips.append(_Clip(
            title, author, kind, _location(_LOCATION.search(rest)),
            re.sub(r"\s+", "", page.group(1)) if page else None, body,
            local.astimezone(timezone.utc).strftime(_ISO), len(acc.clips)))
    acc.files.append({"file": rel, "kind": "kindle", "rows": found, "books": books})


def _contains(longer: str, shorter: str) -> bool:
    inner = _collapse(shorter).casefold().strip(_EDGE)
    return bool(inner) and inner in _collapse(longer).casefold()


def _overlaps(a: _Clip, b: _Clip) -> bool:
    if a.loc and b.loc:
        return a.loc[0] <= b.loc[1] and b.loc[0] <= a.loc[1]
    if a.page and b.page:
        return a.page == b.page
    return not (a.loc or b.loc or a.page or b.page)


def _dedupe_clips(clips: list[_Clip], acc: "_Acc") -> list[_Clip]:
    """The longest of each edited highlight or note (module docstring).

    A clipping that overlaps and contains (or is contained by) several kept
    ones — a highlight extended across two earlier ones — merges them all:
    they are versions of one passage, and the longest stands for it. Ties go
    to the later clipping, the newer edit."""
    groups: dict[tuple, list[_Clip]] = {}
    for c in sorted(clips, key=lambda c: (c.added, c.order)):
        groups.setdefault((c.book, c.author, c.kind), []).append(c)
    survivors = []
    for group in groups.values():
        kept: list[_Clip] = []
        for c in group:
            same = [k for k in kept if _overlaps(c, k)
                    and (_contains(c.text, k.text) or _contains(k.text, c.text))]
            if not same:
                kept.append(c)
                continue
            versions = same + [c]
            winner = max(versions, key=lambda x: (len(_collapse(x.text)), x.added, x.order))
            text = _collapse(winner.text).casefold()
            for v in versions:
                if v is not winner:
                    acc.skipped[SKIP_TWICE if _collapse(v.text).casefold() == text
                                else SKIP_EDITED] += 1
            kept = [k for k in kept if not any(k is s for s in same)] + [winner]
        survivors.extend(kept)
    by_id: dict[str, _Clip] = {}
    for c in sorted(survivors, key=lambda c: (c.added, c.order)):
        if c.external_id() in by_id:
            acc.skipped[SKIP_REPLACED] += 1
        by_id[c.external_id()] = c
    return list(by_id.values())


def _clip_record(c: _Clip, cfg: Config) -> dict:
    detail = {"author": c.author, "kind": c.kind,
              "location": c.loc[2] if c.loc else None, "page": c.page}
    return {
        "stream": "highlight",
        "external_id": c.external_id(),
        "journal_date": base.journal_date_for_iso(c.added, cfg),
        "started_at": c.added,
        "ended_at": None,
        "utc_offset_sec": None,
        "direction": None,
        "title": c.book,
        "body": c.text,
        "value": None,
        "unit": None,
        "value_json": canonical_json({k: v for k, v in detail.items() if v is not None}),
        "media_path": None,
        "origin_app": "kindle",
        "last_modified_at": c.added,
        "participants": [],
    }


# ---------------------------------------------------------------- Goodreads

_GR_REQUIRED = ("book id", "title", "author")


def _cell(row: dict, col: str) -> str:
    return (row.get(col) or "").strip()


def _isbn(raw: str) -> str | None:
    """Goodreads writes ISBNs as Excel formulas so leading zeros survive:
    '="0393635524"', and '=""' for none."""
    s = raw.strip()
    m = re.fullmatch(r'="?([^"]*)"?', s)
    if m:
        s = m.group(1)
    s = s.strip()
    return s or None


def _gr_date(raw: str) -> date | None:
    s = raw.strip()
    if not s:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(s)


def _int(raw: str) -> int | None:
    s = raw.strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _review(raw: str) -> str | None:
    s = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = "\n".join(_collapse(line) for line in s.splitlines())
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s or None


def _read_goodreads(rel: str, text: str, mtime: datetime | None, cfg: Config,
                    acc: "_Acc") -> None:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
    missing = [c for c in _GR_REQUIRED if c not in header]
    if missing:
        raise DomainError(f"{rel} looks like a Goodreads export but has no "
                          f"{', '.join(missing)} column")
    col = {k: header.get(k, "") for k in (
        "book id", "title", "author", "additional authors", "isbn", "isbn13",
        "my rating", "number of pages", "year published", "original publication year",
        "date read", "date added", "bookshelves", "exclusive shelf", "my review",
        "read count")}
    tz = ZoneInfo(cfg.timezone)
    rows = []
    newest: date | None = None
    for lineno, row in enumerate(reader, start=2):
        book_id = _cell(row, col["book id"])
        title = _collapse(_cell(row, col["title"]))
        if not book_id or not title:
            acc.unknown("Goodreads row with no Book Id or Title", f"{rel}:{lineno}")
            continue
        dates = {}
        for key in ("date read", "date added"):
            try:
                dates[key] = _gr_date(_cell(row, col[key])) if col[key] else None
            except ValueError:
                acc.unknown(f"Goodreads row with an unreadable {key.title()}",
                            f"{rel}:{lineno}")
                dates[key] = None
        when = dates["date read"] or dates["date added"]
        if when is None:
            acc.unknown("Goodreads row with neither Date Read nor Date Added",
                        f"{rel}:{lineno}")
            continue
        for d in dates.values():
            if d and (newest is None or d > newest):
                newest = d
        rating = _int(_cell(row, col["my rating"])) if col["my rating"] else None
        author = _collapse(_cell(row, col["author"])) or None
        review = _review(_cell(row, col["my review"])) if col["my review"] else None
        shelves = [s.strip() for s in _cell(row, col["bookshelves"]).split(",")
                   if s.strip()] if col["bookshelves"] else []
        others = [a.strip() for a in _cell(row, col["additional authors"]).split(",")
                  if a.strip()] if col["additional authors"] else []
        year = (_int(_cell(row, col["original publication year"]))
                if col["original publication year"] else None) \
            or (_int(_cell(row, col["year published"])) if col["year published"] else None)
        detail = {"author": author, "additional_authors": others or None,
                  "isbn": _isbn(_cell(row, col["isbn"])) if col["isbn"] else None,
                  "isbn13": _isbn(_cell(row, col["isbn13"])) if col["isbn13"] else None,
                  "shelves": shelves or None,
                  "exclusive_shelf": _cell(row, col["exclusive shelf"]) or None
                  if col["exclusive shelf"] else None,
                  "date_read": dates["date read"].isoformat() if dates["date read"] else None,
                  "date_added": dates["date added"].isoformat() if dates["date added"]
                  else None,
                  "pages": _int(_cell(row, col["number of pages"]))
                  if col["number of pages"] else None,
                  "read_count": _int(_cell(row, col["read count"]))
                  if col["read count"] else None,
                  "year": year}
        noon = datetime.combine(when, time(12), tz)
        rows.append({
            "stream": "book",
            "external_id": f"goodreads:{book_id}",
            "journal_date": when.isoformat(),
            "started_at": noon.astimezone(timezone.utc).strftime(_ISO),
            "ended_at": None,
            "utc_offset_sec": None,
            "direction": None,
            "title": title,
            "body": "\n\n".join(p for p in (author, review) if p) or None,
            "value": float(rating) if rating else None,
            "unit": "stars" if rating else None,
            "value_json": canonical_json({k: v for k, v in detail.items()
                                          if v is not None}),
            "media_path": None,
            "origin_app": "goodreads",
            "participants": [],
        })
    # The export says nothing about when a row changed: its own "as of" does.
    as_of = datetime.combine(newest, time(12), tz).astimezone(timezone.utc) \
        if newest else None
    if mtime is not None and (as_of is None or mtime > as_of):
        as_of = mtime
    stamp = as_of.strftime(_ISO) if as_of else None
    for r in rows:
        r["last_modified_at"] = stamp
    acc.books.extend(rows)
    acc.files.append({"file": rel, "kind": "goodreads", "rows": len(rows),
                      "as_of": stamp})


# ---------------------------------------------------------------- sniffing

def _decode(raw: bytes, rel: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise DomainError(f"{rel} is not valid UTF-8: {e}") from e


def _sniff(raw: bytes) -> str | None:
    """'kindle' | 'goodreads' | None, from the first bytes."""
    head = raw[:_SNIFF_BYTES].decode("utf-8-sig", errors="replace")
    first = head.splitlines()[0] if head.strip() else ""
    names = {c.strip().lower() for c in next(csv.reader([first]), [])} if first else set()
    if {"book id", "title"} <= names and ({"exclusive shelf", "my rating"} & names):
        return "goodreads"
    if _SEPARATOR.search(head) and re.search(r"^-\s*\S.*\|", head, re.M):
        return "kindle"
    return None


class _Acc:
    def __init__(self):
        self.clips: list[_Clip] = []
        self.books: list[dict] = []
        self.skipped: Counter = Counter()
        self.unmapped: Counter = Counter()
        self.examples: dict[str, str] = {}
        self.files: list[dict] = []
        self.unread: list[str] = []

    def unknown(self, what: str, where: str) -> None:
        self.unmapped[what] += 1
        self.examples.setdefault(what, where)


def _junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p.startswith(".") or p.lower() == "__macosx" for p in parts)


def _local_mtime(stamp: float | tuple, cfg: Config) -> datetime | None:
    try:
        if isinstance(stamp, tuple):           # a zip member: naive local time
            dt = datetime(*stamp).replace(tzinfo=ZoneInfo(cfg.timezone))
        else:
            dt = datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _files(target: Path, cfg: Config, acc: _Acc):
    """Yield (rel, read, mtime) for each .txt / .csv candidate, in a stable
    order. Other files are counted in acc.unread."""
    if target.is_dir():
        for p in sorted(target.rglob("*")):
            rel = p.relative_to(target).as_posix()
            if not p.is_file() or _junk(rel):
                continue
            if p.suffix.lower() in (".txt", ".csv"):
                yield rel, p.read_bytes, _local_mtime(p.stat().st_mtime, cfg)
            else:
                acc.unread.append(rel)
        return
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                if info.is_dir() or _junk(info.filename):
                    continue
                if PurePosixPath(info.filename).suffix.lower() in (".txt", ".csv"):
                    yield (info.filename, lambda info=info: zf.read(info),
                           _local_mtime(info.date_time, cfg))
                else:
                    acc.unread.append(info.filename)
        return
    yield target.name, target.read_bytes, _local_mtime(target.stat().st_mtime, cfg)


# ---------------------------------------------------------------- targets

def _kindle_volumes() -> list[Path]:
    """Mounted Kindle e-readers' clippings files on this machine."""
    found = []
    if platforms.is_windows():
        roots = []
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            for letter in string.ascii_uppercase[3:]:          # D: onwards
                root = f"{letter}:\\"
                if kernel.GetDriveTypeW(root) == 2:            # DRIVE_REMOVABLE
                    roots.append(Path(root))
        except (AttributeError, OSError):
            roots = []
        candidates = [r / "documents" / CLIPPINGS for r in roots]
    elif platforms.is_mac():
        candidates = sorted(Path("/Volumes").glob(f"*/documents/{CLIPPINGS}"))
    else:
        user = os.environ.get("USER", "")
        candidates = sorted([*Path("/media", user).glob(f"*/documents/{CLIPPINGS}"),
                             *Path("/run/media", user).glob(f"*/documents/{CLIPPINGS}")])
    for p in candidates:
        try:
            if p.is_file():
                found.append(p)
        except OSError:
            continue
    return found


def default_target(cfg: Config) -> Path:
    """[connectors.reading] path, else the one mounted Kindle's clippings or
    Goodreads export in Downloads. Both present (or several) are refused:
    each is its own import, so pass the one to read."""
    settings = cfg.connector_settings(name)
    if settings.get("path"):
        p = cfg.resolve(settings["path"])
        if not p.exists():
            raise DomainError(f"[connectors.reading] path does not exist: {p}")
        return p
    downloads = platforms.home() / "Downloads"
    found = list(_kindle_volumes())
    try:
        found += sorted(downloads.glob("goodreads_library_export*.csv"))
    except OSError:
        pass
    if len(found) == 1:
        return found[0]
    if found:
        raise base.Ambiguous("found several reading exports — pass the one to import: "
                          + "; ".join(str(p) for p in found))
    raise DomainError(
        f"no reading export found — no Kindle is plugged in (documents/{CLIPPINGS}) "
        f"and no {GOODREADS} is in {downloads}; pass a file or folder "
        f"(docs/reading.md), or set [connectors.reading] path")


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    acc = _Acc()
    alone = target.is_file() and not zipfile.is_zipfile(target)
    content = hashlib.sha256()
    try:
        for rel, read, mtime in _files(target, cfg, acc):
            raw = read()
            kind = _sniff(raw)
            if kind is None:
                if alone:
                    head = raw[:200].decode("utf-8-sig", errors="replace")
                    first = _collapse(head.splitlines()[0] if head.strip() else "")[:80]
                    raise DomainError(
                        f"{rel} is neither a Kindle '{CLIPPINGS}' nor a Goodreads "
                        f"library export — it begins: {first or '(empty)'}")
                acc.unread.append(rel)
                continue
            content.update(f"{rel}\0{hashlib.sha256(raw).hexdigest()}\n".encode("utf-8"))
            text = _decode(raw, rel)
            if kind == "kindle":
                _read_clippings(rel, text, cfg, acc)
            else:
                _read_goodreads(rel, text, mtime, cfg, acc)
    except PermissionError as e:
        raise DomainError(
            f"cannot read {e.filename or target}: permission denied — on a Mac, allow "
            f"the app running this to use that folder (System Settings > Privacy & "
            f"Security > Files and Folders), then run it again") from e
    except zipfile.BadZipFile as e:
        raise DomainError(f"cannot read {target.name} as a zip: {e}") from e
    if not acc.files:
        listing = ", ".join(acc.unread[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} holds no Kindle '{CLIPPINGS}' and no Goodreads library "
            f"export — it has: {listing}")

    clips = _dedupe_clips(acc.clips, acc)
    records = [_clip_record(c, cfg) for c in clips] + acc.books
    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    per_book = Counter(r["title"] for r in records if r["stream"] == "highlight")
    shelves = Counter(_shelf(r) for r in records if r["stream"] == "book")
    spans = {}
    for stream in streams:
        dates = [r["journal_date"] for r in records if r["stream"] == stream]
        if dates:
            spans[stream] = [min(dates), max(dates)]
    return base.empty_scan(
        name,
        source_file=target.name,
        source_hash=base.file_hash(target) if target.is_file() else content.hexdigest(),
        records=records,
        unmapped=[{"what": k, "count": v, "example": acc.examples.get(k)}
                  for k, v in sorted(acc.unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(acc.skipped.items())],
        facts={"files": acc.files, "spans": spans,
               "top_books": [[b, n] for b, n in
                             sorted(per_book.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
               "shelves": dict(sorted(shelves.items())),
               "rated": sum(1 for r in records
                            if r["stream"] == "book" and r["value"] is not None)},
        unread=acc.unread)


def _shelf(rec: dict) -> str:
    return (json.loads(rec["value_json"] or "{}").get("exclusive_shelf")
            or "no shelf")


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    if rec["stream"] == "highlight":
        text = _collapse(rec["body"])
        if len(text) > 50:
            text = text[:49] + "…"
        return f"{rec['journal_date']} {rec['title']} — \"{text}\""
    stars = f", {int(rec['value'])} star(s)" if rec["value"] else ""
    return f"{rec['journal_date']} {rec['title']} ({_shelf(rec)}{stars})"


_LABELS = {"kindle": "Kindle clippings", "goodreads": "Goodreads library export"}


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    files = facts.get("files") or []
    parts = []
    for f in files[:4]:
        what = (f"{f['rows']} clipping(s) from {f['books']} book(s)" if f["kind"] == "kindle"
                else f"{f['rows']} book(s)")
        where = f" {f['file']}" if f["file"] != scan["source_file"] else ""
        parts.append(f"{_LABELS[f['kind']]}{where}: {what}")
    lines = [f"reading: {scan['source_file']} (" + "; ".join(parts)
             + (f"; +{len(files) - 4} more file(s)" if len(files) > 4 else "") + ")"]
    spans = facts.get("spans") or {}
    if "highlight" in spans:
        lo, hi = spans["highlight"]
        lines.append(f"  clippings span {lo}..{hi} — dated by the e-reader's clock, read "
                     f"in the home timezone")
    if facts.get("top_books"):
        lines.append("  most highlighted: " + ", ".join(
            f"{b} ({n})" for b, n in facts["top_books"]))
    if facts.get("shelves"):
        lines.append("  shelves: " + ", ".join(f"{s} {n}" for s, n in
                                               facts["shelves"].items())
                     + f" | rated {facts.get('rated', 0)}")
    for f in files:
        if f["kind"] == "goodreads" and f.get("as_of"):
            lines.append(f"  {f['file']} is as of {f['as_of'][:10]} (its file time or "
                         f"newest date) — a later export updates what this one stored")

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    shown = False
    for stream in streams:
        n = [r for r in new if r["stream"] == stream]
        u = [r for r in upd if r["stream"] == stream]
        if not (n or u):
            continue
        shown = True
        recs = n or u
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  {stream}: {len(n)} new, {len(u)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
    if not shown:
        lines.append("  nothing new — the export matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} unchanged record(s) re-dated to this export")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} record(s) listed twice")
    if tail:
        lines.append("  " + ", ".join(tail))

    books = [c for c in plan["conflicts"] if c["stream"] == "book"]
    if books:
        lines.append(f"  kept existing: {len(books)} book(s) differ from what a later "
                     f"export already stored — an older export cannot overwrite them")
    other = [c for c in plan["conflicts"] if c["stream"] != "book"]
    for c in other[:5]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:20]}…: differs from "
                     f"the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    if len(other) > 5:
        lines.append(f"  … and {len(other) - 5} more conflict(s)")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not understood: {u['count']} × {u['what']}"
                     + (f" (e.g. {u['example']})" if u.get("example") else ""))
    unread = scan.get("unread") or []
    if unread:
        lines.append(f"  not read: {len(unread)} other file(s), e.g. "
                     + ", ".join(sorted(PurePosixPath(u).name for u in unread)[:4]))
    return lines

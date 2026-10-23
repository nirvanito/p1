# Reading: Kindle highlights and the Goodreads library

What you underline is what stayed with you, and what you shelve is what you
meant to get to. Both already sit in plain files: a Kindle e-reader keeps
every highlight and note in `My Clippings.txt`, and Goodreads exports the
whole library as one CSV. The `reading` connector reads those files. It never
logs in anywhere and never sends anything.

Records land in the **observed tier**: bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited. See
[docs/observed-tier.md](observed-tier.md).

## Getting the files

### Kindle: My Clippings.txt (both combos)

The e-reader writes `documents/My Clippings.txt` itself, whichever computer
it is plugged into. Connect it by USB:

- **Windows (combo 1):** it shows up in File Explorer as **Kindle** —
  `Kindle\documents\My Clippings.txt`. Older Kindles mount as a drive letter
  (`E:\documents\...`), and `observe reading` with no path finds the file on
  any removable drive. Newer ones (USB-C models since about 2021) appear as a
  *device*, not a drive: copy the file out to a folder first.
- **Mac (combo 2):** older Kindles mount at `/Volumes/Kindle` and are found
  the same way. USB-C models speak MTP, which macOS does not mount: copy the
  file off with a tool that does (Android File Transfer, OpenMTP), then
  import the copy.

**Kindle apps do not write this file.** Highlights made in the Kindle app on
the Samsung, the iPhone or the computer live in the Kindle notebook on
Amazon's site (read.amazon.com/notebook), which this connector does not read.

### Goodreads: goodreads_library_export.csv (both combos)

Website only — the Goodreads apps have no export: **My Books** → under
*Tools* in the left column, **Import and export** → **Export Library**. When
the export is ready, a link "Your export from <date>" downloads
`goodreads_library_export.csv`.

**Not verified against a real device or account:** the menu labels, how a
USB-C Kindle shows up on each system, and the CSV's exact column list are as
documented by Amazon/Goodreads and their users; the demo files follow them.
The first real dry run will show what was read (checklist below).

## Importing

Dry-run first — one file, a folder holding either or both (a Kindle's own
drive works: only `.txt` and `.csv` files are opened, each sniffed by its
content), or a `.zip`:

```bash
.venv/Scripts/python -m lifecrm observe reading "E:\documents\My Clippings.txt"
.venv/Scripts/python -m lifecrm observe reading ~/Downloads/goodreads_library_export.csv
```

```
reading: My Clippings.txt (Kindle clippings: 10 clipping(s) from 2 book(s))
  clippings span 2025-09-14..2026-10-20 — dated by the e-reader's clock, read in the home timezone
  most highlighted: The Overstory: A Novel (6), The Martian (1)
  highlight: 7 new, 0 changed | 2025-09-14..2026-10-20 | e.g. 2026-10-20 The Overstory: A Novel — "Loved the ending."
  skipped: 1 × bookmark (a place in a book, no text)
  skipped: 1 × earlier, shorter version of a highlight or note that was extended later (the longest is kept)
  skipped: 1 × highlight the publisher's clipping limit left out (Kindle wrote a placeholder, not the text)
dry run — nothing written. Re-run with --apply to write.
```

```
reading: goodreads_library_export.csv (Goodreads library export: 4 book(s))
  shelves: read 2, to-read 2 | rated 2
  goodreads_library_export.csv is as of 2026-10-21 (its file time or newest date) — a later export updates what this one stored
  book: 4 new, 0 changed | 2025-09-16..2026-10-20 | e.g. 2026-10-20 The Overstory (read, 5 star(s))
dry run — nothing written. Re-run with --apply to write.
```

Then `--apply`; `lifecrm imports retract <uuid>` undoes a run. With no path,
`observe reading` takes `[connectors.reading] path`, else the one plugged-in
Kindle's clippings or `goodreads_library_export*.csv` in Downloads — both
present is refused (each is its own import: pass the one to read).

```toml
[connectors.reading]
path = "~/Documents/Kindle/My Clippings.txt"
```

## What gets imported

### `highlight` — one record per Kindle highlight or note

| field | what |
|---|---|
| title | the book, as Kindle names it — the title line is `Title (Author)`, and the author is its *last* parenthesised part, so `The Name of the Wind (Kingkiller Chronicle, Book 1) (Patrick Rothfuss)` keeps the series in the title |
| body | the highlighted text, or the note |
| started_at | `Added on`, UTC |
| value_json | `author`, `kind` (`highlight` / `note`), `location` (`180-182`; an old Kindle's `180-82` written out), `page` |
| external_id | `kindle:` + sha1 of book, author, kind and where it starts (the location, else the page, else the text) |
| participants | none |

**Times.** `Added on` is the e-reader's own clock and names no zone: the
home timezone reads it, the configured rollover applied (a highlight at
00:40 belongs to the evening before). Month and day names are read by the
connector itself, never through the machine's locale. US
(`Tuesday, October 6, 2026 10:12:33 PM`), UK (`Tuesday, 6 October 2026
22:12:33`) and older-Kindle (`Tuesday, October 6, 2012, 10:12 PM`, with
`Loc. 180-82`) spellings are all read.

**Edited highlights.** The file is append-only: extend a highlight and
Kindle adds the longer one beside the original. Within a book and kind, a
clipping whose place overlaps another's and whose text contains it is the
same passage edited — **the longest is kept**, the others counted. A
highlight extended across two earlier ones merges all three. Extended
*forward*, a highlight keeps its start and therefore its id, so re-importing
updates the stored record to the longer text; extended *backwards* (a new
start), it arrives as a new record and the shorter one already imported
stays. Two clippings at one start whose texts differ — an edited note — are
one record, the latest kept.

**Skipped and counted:** bookmarks (a place, no text), the
`<You have reached the clipping limit for this item>` placeholder a
publisher's limit leaves instead of the text, empty clippings, and exact
duplicates.

**Other languages.** Only English details lines are read. A Kindle set to
German, French, Spanish, … writes `- Ihre Markierung bei Position …`; those
clippings are counted as *not understood*, with the details line as the
example (never the highlighted text), and not guessed at. Set the Kindle to
English: new clippings are then written in English (the ones already in the
file stay as they are).

### `book` — one record per Goodreads book

| field | what |
|---|---|
| title | Title |
| body | Author, then a blank line and the review (My Review; `<br/>` becomes a line break, other markup removed) |
| value / unit | My Rating, `stars` — 0 (not rated) is no value |
| started_at / journal_date | Date Read at local noon, else Date Added: dates, not instants, placed where every rollover keeps the day |
| value_json | `author`, `additional_authors`, `isbn` and `isbn13` (Goodreads' `="…"` Excel quoting removed), `shelves` (Bookshelves), `exclusive_shelf` (`read` / `currently-reading` / `to-read`), `date_read`, `date_added`, `pages`, `read_count`, `year` (original publication year) |
| external_id | `goodreads:<Book Id>` |
| participants | none |

Not imported: Average Rating (the community's, not yours), Private Notes,
Spoiler, Owned Copies, and older exports' purchase columns.

**No handles** in either stream: an author is not a person you know, and a
handle is for someone the owner might link to an entity. Full-text search
finds an author in a book's `body`; a highlight carries its author in
`value_json` (searched by title — the book — and text).

## Re-importing, and what wins

- **Highlights:** `last_modified_at` is `Added on`. An unchanged file is
  identical; a forward-extended highlight updates (see above).
- **Books:** the export says nothing about when a row changed, so every
  book's `last_modified_at` is the export's own *as of* — the file's
  modification time, or its newest date when that is later. A later export
  wins: a book moved to read, a new rating or review updates the stored
  record, audited. An older export overwrites nothing; differences are
  counted on one line ("kept existing: N book(s) differ from what a later
  export already stored"). Keep the file's date when copying it (a plain
  copy does).

## Nothing is mirrored

`My Clippings.txt` holds only what this e-reader saw — a new Kindle starts an
empty file, and deleting a highlight on the device never touches the file
anyway. A book taken off a Goodreads shelf is a change of mind, not a book
never read. The archive remembers both.

## Known limitations

- **Kindle app highlights** (phone, tablet, computer) are not in
  `My Clippings.txt` and are not read.
- **Highlights made abroad** land on the home-timezone day: the e-reader's
  clock has no zone in the file.
- **Non-English Kindles** are counted, not imported (see above).
- **A highlight extended backwards** after it was imported leaves the shorter
  version in the archive beside the new one.
- **Goodreads only:** StoryGraph, Libib or LibraryThing exports are not
  recognised (the dry run names the CSV's first line).

## First real import — checklist

1. Copy `My Clippings.txt` off the Kindle (or point at the mounted drive);
   export the Goodreads library.
2. Dry run each. Kindle: the clipping count should match roughly what you
   remember; "not understood" should be empty — a count there means a
   language or a details line this reader does not know: stop and report
   the example it shows.
3. Spot-check one highlight's date against the Kindle (Notes & Highlights
   in the book) and one book's shelf and rating against Goodreads.
4. `--apply`, then run each again: everything identical.
5. `lifecrm doctor` stays green.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe reading [<My Clippings.txt | goodreads_library_export.csv | folder | .zip>]
.venv/Scripts/python -m lifecrm observe reading <target> --apply
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
```

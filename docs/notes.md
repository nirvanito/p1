# Notes import (folders, Google Keep, Apple Notes)

Notes are where the in-between things live — the packing list from a trip,
the gift ideas, the outline of a deck, the book list. None of the apps that
hold them has an API worth the name, but each one has a file, and the `notes`
connector reads three kinds:

| source | what you point it at | typical owner |
|---|---|---|
| **a folder** of `.md` / `.markdown` / `.txt` | an Obsidian vault, an exported Apple Notes folder, Samsung Notes saved as text files | anyone |
| **Google Keep** | a Google Takeout zip (Keep only), or the folder it unpacks to | Windows laptop + Samsung phone |
| **Apple Notes** | `NoteStore.sqlite` on a Mac | MacBook + iPhone |

Everything lands in the **observed tier** as stream `note`, one observation
per note: bulk records that never pass the `/log` review gate, are never
asserted as fact, and are always cited. Nothing here can touch `entries`,
`measurements` or `mentions`. See [observed-tier.md](observed-tier.md).

## Getting the notes out

### Windows laptop + Samsung phone

**Google Keep** — the one Android notes app with a real export.

1. Go to <https://takeout.google.com>, click *Deselect all*, tick **Keep**.
2. *Next step* → *Export once*, `.zip`, 2 GB. Create export.
3. A download link arrives by email. Save the zip to `Downloads` — that is
   where `lifecrm observe notes` looks when given no path.

**Samsung Notes** — has no bulk export. Two ways that work:

- Per note: open it → ⋮ → *Save as file* (or *Share*) → **Text file**, and
  save it into a OneDrive folder (for example `OneDrive\Notes\Samsung`). The
  connector reads that folder like any other. The file name becomes the
  title.
- Or move the notes you care about into Google Keep and use Takeout.

Samsung's own `.sdocx` files and Smart Switch backups are proprietary and
encrypted; they are not supported.

**Obsidian / any markdown folder** — point the connector at the vault folder
(the one that holds `.obsidian\`).

### MacBook + iPhone

**Apple Notes** — nothing to export. With iCloud Notes on, the iPhone's
notes sync into the Mac's own store:

```
~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite
```

The connector copies it through SQLite's backup API (safe while Notes is
running) and never opens it for writing. macOS protects that folder: give
the terminal app you run lifecrm from **Full Disk Access** (System Settings →
Privacy & Security → Full Disk Access → add Terminal or iTerm, then restart
it). Without it the import stops with one line saying exactly that.

Notes kept only "On My iPhone" do not sync to the Mac and are not in this
store. Locked notes are encrypted with their own password; they are counted,
never read.

**Obsidian in iCloud Drive** — point at the vault folder under
`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/<vault>`. Notes that
"Optimize Mac Storage" has evicted show up as `.<name>.md.icloud`
placeholders; they are counted as still present (so mirroring never deletes
them) but not imported until you open the folder in Finder and they download.

## Importing

Dry-run first — always. It is the only place a wrong date or a misread body
is cheap to notice.

```bash
python -m lifecrm observe notes "C:\Users\me\Downloads\takeout-20261021T120000Z-001.zip"
```

```
notes: takeout-20261021T120000Z-001.zip (Google Keep Takeout; 5 note(s) in the source)
  read from: C:\Users\me\Downloads\takeout-20261021T120000Z-001.zip
  account: alex.rivera@example.com (from the one address on every shared note)
  notes span 2022-09-26..2026-10-05 — check the oldest against what the app shows
  note: 4 new, 0 changed | 2022-09-26..2026-10-05 | e.g. 2026-10-05 "Book list": Reading now: …
  handles: 2 address(es) notes are shared with — review with 'lifecrm handles list --suggest'
  skipped: 1 × note in Keep's trash (deleted — not kept)
dry run — nothing written. Re-run with --apply to write.
```

Read the span. If the oldest note does not match what the app shows, stop —
that is the signal something is being dated wrong. Then write:

```bash
python -m lifecrm observe notes <same path> --apply
```

The command prints an import uuid; `lifecrm imports retract <uuid>` undoes
what the run added (a run that removed notes prints the snapshot that undoes
the removal). (Run `python` as `.venv\Scripts\python.exe` on Windows and
`.venv/bin/python` on a Mac.)

### With no path

`python -m lifecrm observe notes` picks, in order:

1. `[connectors.notes] path` in `config.toml`, when set — config always wins;
2. on a Mac, the Apple Notes store above;
3. the newest `takeout-*.zip` in `~/Downloads` that holds Keep notes.

```toml
[connectors.notes]
path = '~/OneDrive/Notes/Obsidian'     # a folder, a Takeout zip, or a NoteStore.sqlite
keep_account = "you@gmail.com"         # see "Deleted notes" below
```

## What gets imported

| | folder | Google Keep | Apple Notes |
|---|---|---|---|
| **title** | front matter `title:`, else the first line if it is a heading, else the file name | the note's title (none for an untitled note) | the note's title (its first line) |
| **body** | the text, front matter removed | the text, or a checklist as `- [x] item` lines | the text; inline images, tables and drawings are dropped and counted |
| **written** (`started_at`) | front matter `created:` (or `date:`), else the file's modified time | `createdTimestampUsec`, else the last edit | the creation date |
| **last edit** (`last_modified_at`) | the file's modified time | `userEditedTimestampUsec` | the modification date |
| **detail** (`value_json`) | relative path, format, the parsed front matter | file, labels, archived, pinned, colour, checklist counts, web links, attachment names | folder, account (`iCloud`), pinned, attachment count |
| **origin_app** | `obsidian` or `notes_folder` | `google_keep` | `apple_notes` |

Deliberate choices worth knowing:

- **Front-matter dates are local.** `created: 2026-10-01 14:30` means 14:30
  where you live. A bare `created: 2026-10-01` is read as local noon, so the
  note stays on that journal day instead of sliding to the previous one under
  the rollover hour.
- **Only the first line can be the title.** A `## Packing` halfway down a note
  called *Bled trip* is a section, not the note's name.
- **Front matter is parsed, not executed.** `key: value`, `[a, b]` lists and
  `- item` lists are understood; anything deeper is kept as raw text under its
  key. Dates it cannot read (`created: last Tuesday`) fall back to the file
  time and are counted under "not imported".
- **Text encodings.** UTF-8 with or without a BOM, and UTF-16 with a BOM
  (Windows Notepad's "Unicode"). Anything else is counted and skipped, never
  guessed at.
- **Skipped, not imported:** `.obsidian/`, `.trash/`, `.git/` and every other
  dot-folder and dotfile; empty notes; images, PDFs and other non-note files
  (counted per extension).
- **Media stays out of the database.** Keep attachments are recorded by file
  name; Apple Notes attachments only as a count.
- **Archived Keep notes are kept**, flagged `archived`. Trashed ones are not.

## Identity

| source | `external_id` |
|---|---|
| folder | `file:<folder name>/<path inside it>` |
| Google Keep | `keep:<json file name without .json>` |
| Apple Notes | `apple:<ZIDENTIFIER>` (the note's own UUID) |

Re-importing is always safe: an unchanged note is skipped, an edited one
updates (its last edit is newer), and a changed note that is *not* newer — an
old backup copied over a newer file — is a **conflict**: never applied,
surfaced in the report.

The folder's own name is part of the id, so two vaults that both hold an
`Inbox.md` stay apart, and the same vault synced to a second machine keeps
its ids. Point the connector at the same folder every time — the vault root,
not a subfolder of it one day and the root the next.

A Keep or folder note is identified by its file name, which follows its
title. **Renaming a note reads as one note deleted and one added.** That is
the only identity those sources have.

## Deleted notes: the archive mirrors the source

As with texts and AI chats (decision #2, 2026-09-24), the archive mirrors
the source: a note deleted at the source is removed by the next import. The
dry run shows what would go:

```
  remove: 1 no longer in the source (deleted or trashed) — 1 note | from Grocery list (1)
```

| source | deleted means | still present, though not imported |
|---|---|---|
| folder | the file is gone, or moved into `.trash/` | an empty note, a file that is not UTF-8, an iCloud placeholder, a file that could not be read |
| Google Keep | gone from the export, or in Keep's trash | a note file that is not valid JSON |
| Apple Notes | gone, in *Recently Deleted*, or marked for deletion | a locked note, a note whose text has not downloaded, one that could not be decoded |

The guards are the shared ones (docs/texts.md):

- **Only this account.** Removal is scoped to what the same account first
  imported: for a folder, its full path; for Apple Notes, the store's own
  Core Data UUID (so a second Mac never removes the first's notes); for Keep,
  the Google account. A folder import never removes Keep notes, and the
  reverse.
- **Keep does not say whose export it is.** The account is
  `[connectors.notes] keep_account` when set; otherwise the one address that
  appears on *every* shared note (yours: you are on each of them, as owner or
  collaborator). With no shared notes, or only one, it cannot be told — the
  import then removes nothing and the report says so. Set `keep_account` to
  mirror an export with no shared notes.
- **A split Takeout is not mirrored.** A large export arrives as
  `takeout-…-001.zip`, `-002.zip`, …; if another part also holds Keep notes,
  importing one part would read the rest as deleted. The report says so —
  extract every part into one folder and import the folder.
- **Never most of the archive at once.** Removing half or more of that
  account's archive, or five or more notes that are over a tenth of it, in
  one import is refused without `--allow-mass-removal`.
- **Snapshot first** (`backups/pre-removal-<time>.db`, with the undo
  `lifecrm restore "<that snapshot>"` printed), and a note a confirmed journal
  entry cites is hidden, not deleted.

A note recovered at the source (restored from the trash) comes back on the
next import: as new, or — if an entry cited it, so it was only hidden — as
the same row, active again.

## Attribution

The connector emits **handles, never names**. Only Keep has any: the email
address of everyone a note is shared with (the owner is the account, not a
participant). Resolving one is a join — every shared note attributes at once:

```bash
python -m lifecrm handles list --suggest
python -m lifecrm handles link email:jake.morales@example.com --entity 3
```

Folder notes and Apple Notes carry no participants.

## Known limitations

- **Timezone.** Journal dates use your configured home timezone; a note
  written abroad lands on the home-timezone day. File times and Keep's
  timestamps say *when*, never *where*.
- **A folder note without a `created:` date is dated by its last edit**, so
  editing an old note moves it to today. Add `created:` front matter (Obsidian
  templates can do it) where the original date matters.
- **Apple Notes formatting.** Checklists, headings and tables come through as
  their plain text; drawings, scans and images are only counted. Shared
  Apple notes do not name their collaborators here.
- **Apple Notes column names vary by macOS version**; the connector looks
  each one up (`ZTITLE1`/`ZTITLE`, `ZCREATIONDATE3`/`ZCREATIONDATE1`/
  `ZCREATIONDATE`, …) rather than assuming a layout.

## Real-device checklist (unverified until run on real exports)

These were written from the formats as documented and tested on generated
samples (`lifecrm/demo/sources/notes.py`). Check each on the first real import
and note the result here:

- [ ] Keep Takeout: field names (`createdTimestampUsec` present on current
      exports?), file naming for untitled notes and name collisions, whether
      `sharees` lists the owner with `isOwner: true` on every shared note.
- [ ] Samsung Notes: the exact menu path for *Save as file → Text file*, and
      whether the file name is the note title.
- [ ] Apple Notes (macOS 13–15): note title column, creation-date column, the
      trash folder's `ZFOLDERTYPE` (expected 1), and that `ZDATA` decodes for
      notes with tables and checklists.
- [ ] Apple Notes: the Full Disk Access message appears (and nothing else)
      when the terminal lacks it.
- [ ] Obsidian on iCloud Drive: evicted notes really appear as
      `.<name>.md.icloud`.

## Commands

```bash
python -m lifecrm connectors                       # what's available, last run
python -m lifecrm observe notes [<path>]           # dry run
python -m lifecrm observe notes [<path>] --apply
python -m lifecrm imports list
python -m lifecrm imports retract <uuid>
python -m lifecrm handles list --suggest
```

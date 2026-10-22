# Contacts (address book)

Texts, calls, calendar invites and chats arrive as **handles** — a phone
number, an email address — never as names. The address book is where those
handles have names. The `contacts` connector imports it into the observed
tier, one `contact_card` observation per card, and gives every number and
address on a card that card's name. That name is what
`handles list --suggest` matches against your entities, so one decision —
"this is Jake" — attributes his card, his texts and his calls at once.

Like every observed-tier source it is cited, never asserted, and it can never
write `entries`, `measurements`, `mentions` or entity aliases. Nothing is
linked automatically. See [docs/observed-tier.md](observed-tier.md).

## Getting the address book

**Windows laptop + Samsung (Android) phone.** Your contacts live in your Google
account (Samsung Contacts syncs to it). On the laptop, open
contacts.google.com → *Export* → **vCard (for iOS Contacts)** → *Export*. The
download is `contacts.vcf` in your Downloads folder.

The phone can also export on its own: Samsung Contacts → ☰ → *Manage
contacts* → *Import or export contacts* → *Export* → internal storage. That
file is vCard 2.1 (quoted-printable, bare `TEL;CELL:` types); it reads the
same way. Copy it to the laptop (OneDrive works).

**MacBook + iPhone.** Nothing to export: with iCloud Contacts on, the Mac
already holds the iPhone's address book in
`~/Library/Application Support/AddressBook`, and the connector reads it there.
macOS protects that folder, so the terminal you run `lifecrm` from needs
**Full Disk Access** (System Settings → Privacy & Security → Full Disk Access →
add Terminal, or iTerm, or your Python). Without it the import stops with a
one-line message saying exactly that.

Also accepted, on either machine:

- any `.vcf` — macOS Contacts *File → Export → Export vCard…*, an iCloud.com
  export, Outlook — or a folder or `.zip` of them (Google Takeout's
  `Contacts/` folder: the same card in several label files is read once);
- one `AddressBook-v22.abcddb` store on its own;
- an **iPhone backup** folder (the one holding `Manifest.db`; see
  [docs/texts.md](texts.md)) — its `AddressBook.sqlitedb` is found through the
  manifest. Encrypted backups cannot be read; the connector says so.

## Importing

Dry-run first; it is the review step:

```
.venv\Scripts\python.exe -m lifecrm observe contacts                 # Windows
.venv/bin/python -m lifecrm observe contacts                         # Mac
.venv\Scripts\python.exe -m lifecrm observe contacts "%USERPROFILE%\Downloads\contacts.vcf"
```

With no path it uses `[connectors.contacts] path` from config.toml when set;
otherwise, on a Mac, the Contacts folder; otherwise the newest `.vcf` in your
Downloads folder. If it finds nothing it says where it looked.

```
contacts: contacts.vcf (vCard 3.0, google_contacts; 7 card(s))
  dates: 7 card(s) carry no date of their own — dated by the export (2026-10-21)
  contact_card: 7 new, 0 changed | 2026-10-21..2026-10-21 | e.g. Veracruz All Natural — +15125550177; Address: 1704 E Cesar Chavez St, Austin, TX 7…
  not mirroring deletions: this source does not say which account it is, so deletions cannot be scoped to it safely
  to mirror deletions from this export, name its account: [connectors.contacts] account in config.toml (docs/contacts.md)
  handles: 6 email, 7 phone — each named from its card; review with 'lifecrm handles list --suggest'
  not imported: CATEGORIES (7)
dry run — nothing written. Re-run with --apply to write.
```

Read the card count against what your phone shows, then add `--apply`.
Re-importing is always safe: an unchanged card is skipped, a changed one
updates (newer wins), and `lifecrm imports retract <uuid>` undoes what a run
added (what it removed comes back from the snapshot it prints).

Then resolve the people:

```
.venv\Scripts\python.exe -m lifecrm handles list --suggest
.venv\Scripts\python.exe -m lifecrm handles link phone:+15125550142 --entity 12
.venv\Scripts\python.exe -m lifecrm handles link email:jake.morales@example.com --entity 12
```

## What a card becomes

| field | from the card |
|---|---|
| `title` | the name shown in Contacts ("Dr. Rebecca Ellis"; a company card: the company) |
| `body` | text a search should find: "Director of Product at Acme Robotics", "Lives in Austin, TX", "Birthday: March 14, 1993", and the card's notes verbatim ("Grew up in El Paso…") |
| `value_json` | `{phones, emails, addresses, birthday, org, title, nickname, urls}` — phones as typed plus their normalized handle; labels in one vocabulary (`mobile`, `home`, `work`, `iphone`, … or your custom label) |
| participants | every phone number and email address on the card, role `participant` |
| `external_id` | the card's own id: vCard `UID` (or Apple's `X-ABUID`), the Mac's `ZUNIQUEID`, `ios:<device>:<row>` for an iPhone backup |

Phone numbers are normalized exactly as texts and calls normalize them
(`+1 512-555-0142`, `(512) 555-0142` and `5125550142` are one handle), and an
extension or dialing pause (`x12`, `;ext=5`, `,,123`) is cut off — texts never
come from an extension. A value that is not a number at all (`*86`) stays on
the card but gets no handle; the report counts it.

A number on two cards (a shared landline) is named `Dad / Mom`, both names,
rather than one picked by guesswork; `--suggest` matches each name.

**Ids for exports that have none.** Google and Samsung exports carry no card
id. There the id is derived from the name — and from the name plus its
numbers when two different cards share a name. So renaming such a card
imports as a removal plus an addition, and adding a second "Jake Morales"
re-keys both. The same card appearing twice (Takeout label files) is read
once.

**Dates.** A card is dated by its **last change**: `REV` in a vCard, the
modification date in a Mac or iPhone store (creation date only if a store
lacks one). A card edited today — a new number, a note — shows up today,
which is when that information appeared, and the same card read from the
Mac store and from an Apple vCard export gets the same date. A card with no
date at all (Google, Samsung) is dated by the export file's modification
time, because "as of this export" is all the file says; re-importing a
*newer* export of such a file re-dates every card, and the report counts
those as "only re-dated" so they do not read as edits.

Not imported, and counted by name in the report: photos (media stays outside
the database), group memberships (`CATEGORIES`), social profiles, related
names, anniversaries, and any other property not in the table above.

## Deleted cards: the address book is mirrored, per account

A contacts export is your whole address book, so a card you delete in
Contacts is removed from the archive by the next import — the same decision
as texts and AI chats (docs/next-brief.md, decision #2). The guards are the
shared ones: only cards **this account's** imports created can be removed;
half or more of the address book, or five or more cards that are over a
tenth of it, at once is refused without `--allow-mass-removal` (an export of
one Google account, not all of them, or a vCard of a single card, looks
exactly like mass deletion); a database snapshot (`backups/pre-removal-<time>.db`)
is taken first and the undo, `lifecrm restore "<that snapshot>"`, is printed
— `imports retract` cannot bring a removed card back; and a card a journal
entry or a relationship cites is hidden, not deleted, and active again once
the address book holds it again.

Which account a source is:

| source | account |
|---|---|
| a Mac's Contacts folder | `mac-addressbook:<id>` — the top-level store's own UUID, one per Mac user |
| one `.abcddb` store passed alone | `mac-store:<Sources folder id>` — never touches the other stores' cards |
| an iPhone backup | `iphone:<device id>` (the backup folder's name) |
| a `.vcf` | only what config.toml says — a vCard does not say whose it is |

So a vCard mirrors only once you name it:

```toml
[connectors.contacts]
account = "google:alex.rivera@example.com"   # any stable label for this address book
path = '~/OneDrive/Contacts/contacts.vcf'     # the export to mirror
```

Without `account`, nothing is ever removed and the dry run says so. With
`account` set, `observe contacts` with no path insists on `path` (or an
explicit file): mirroring against "the newest .vcf in Downloads" could pick
up a single card someone emailed you and remove the rest.

Use **one route per address book**. The iPhone backup's address book and the
Mac's Contacts folder hold the same iCloud cards under different ids, so
importing both shows each person twice; on a Mac, import the Contacts folder.

## Known limitations

- Unverified on real exports: Google's exact vCard spelling (believed to carry
  no `UID` and no `REV`), Samsung's 2.1 export, and the Mac store's columns
  across macOS versions (every column is checked before use, so a missing one
  reads as empty rather than failing). The first real import's dry run is the
  check — compare the card count and a few cards with what Contacts shows.
- Only the first `ORG` component is kept (the department is dropped), and a
  vCard 2.1 line folded at a space keeps that space.
- Several Macs importing into one database are fine only if each is its own
  user account (they get separate `mac-addressbook:` ids).

## Commands

```bash
python -m lifecrm observe contacts [path]            # dry run
python -m lifecrm observe contacts [path] --apply
python -m lifecrm handles list --suggest
python -m lifecrm handles link <kind:value> --entity <id>
python -m lifecrm imports retract <uuid>
```

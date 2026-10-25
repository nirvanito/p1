# Texts from an iPhone (iMessage, SMS, RCS)

The iPhone has no API for its messages. There are two ways in, and both end
in the same Messages database schema:

| connector | reads | where it works |
|---|---|---|
| `ios_messages` | the iPhone's `sms.db`, out of an **unencrypted** local backup | Windows (Apple Devices / iTunes) or a Mac (Finder) |
| `mac_messages` | the Mac's own `~/Library/Messages/chat.db`, kept in sync by Messages in iCloud | a Mac signed in to Messages |

Neither talks to the phone, reads a live database in place, or sends
anything: every database is copied through SQLite's backup API (or, for a
backup's files, copied whole) and read from the copy. Calls are a separate
connector — see `docs/calls.md`.

Encryption at rest is decided *not required* (2026-09-24): this is a
personal tool on a personal machine, not a released product. The backup must
be unencrypted only because an encrypted one cannot be read without its
password.

## Making the backup (Windows)

1. Install **Apple Devices** from the Microsoft Store (or iTunes on older
   setups) and connect the iPhone by cable. Trust the computer on the phone.
2. Under *Backups*, choose **Back up all of the data on your iPhone to this
   computer**, and make sure **Encrypt local backup is unticked**. An encrypted
   backup cannot be read without its password; the connector says so and stops.
3. Back up now. You do not need to find where it went — see below.

## Making the backup (Mac, Finder)

1. Connect the iPhone by cable (or over Wi-Fi once paired) and select it in
   the Finder sidebar, under *Locations*. Trust the computer on the phone.
2. On the *General* tab, under *Backups*, choose **Back up all of the data on
   your iPhone to this Mac** and make sure **Encrypt local backup is
   unticked**.
3. Click **Back Up Now**. Finder writes it to
   `~/Library/Application Support/MobileSync/Backup/<device id>/`, which is
   where `observe ios_messages` looks.
4. Reading that folder needs **Full Disk Access** — see below.

On a Mac, most people will not need a backup at all: `mac_messages` reads the
same conversations straight from Messages.

## Importing

Dry-run first; it is the review step:

```
.venv\Scripts\python.exe -m lifecrm observe ios_messages
```

With no path, it looks where Apple's tools write backups —
`%USERPROFILE%\Apple\MobileSync\Backup` (Apple Devices), then
`%APPDATA%\Apple Computer\MobileSync\Backup` (desktop iTunes), then a Mac's
`~/Library/Application Support/MobileSync/Backup` — and uses the backup it
finds there. If there are several (two phones, an iPad, or an old one), it
lists them with device name and date and asks you to pass one; it never
guesses which phone you meant. A path works too: the `<device id>` folder
that holds `Manifest.db`, or the `Backup` folder above it when that holds
just one. To make the choice once — for the daily `collect` job too — name
the backup in `config.toml`:

```toml
[connectors.ios_messages]
path = "~/Library/Application Support/MobileSync/Backup/00008110-001A2B3C4D5E6F"
```

The report says which folder it read, names the device and backup date, then
shows per stream how many records are new or changed, the date span, and the
latest one as a sample. It lists what was skipped and what was not understood,
with counts. **Read the samples**: garbled text, a wrong year, or a large
"could not be decoded" count means stop and look. When it looks right, add
`--apply`. `--json` carries the same facts for scripts.

Re-running on the same backup is a no-op; a later backup adds only what is new.
(The no-op check hashes the database *and* its `-wal`, so a backup whose new
messages sit only in the write-ahead log is not mistaken for an unchanged one.)

Also accepted: a folder holding `sms.db` (or a Mac's `chat.db`) with an
optional `AddressBook.sqlitedb` beside it, or a bare `sms.db`/`chat.db` file.
A bare file is copied through SQLite's backup API, never opened in place.
(A Mac's own `chat.db` belongs to `mac_messages`, below — under
`ios_messages` its deletions would be mirrored against the phone's.)

## Reading a Mac's Messages directly (`mac_messages`)

With **Messages in iCloud** on (Messages > Settings > iMessage > *Enable
Messages in iCloud*), the Mac's `~/Library/Messages/chat.db` holds the same
conversations as the phone and stays current on its own. Turn on **Text
Message Forwarding** on the iPhone (Settings > Apps > Messages) so SMS/RCS
threads reach the Mac too.

```
.venv/bin/python -m lifecrm observe mac_messages            # dry run
.venv/bin/python -m lifecrm observe mac_messages --apply
```

With no path it reads this Mac's `~/Library/Messages/chat.db`. A path works
too: `~/Library/Messages`, or a copied `chat.db` — a copied `Library` folder
keeps its Contacts beside it and is read the same way; a lone `chat.db`
imports without names. An iPhone backup folder or an `sms.db` is refused with
the `ios_messages` command to use instead.

It is `ios_messages`' reader, unchanged — same streams, same tapbacks,
thread events, edits, unsends and Recently Deleted handling. What is
different:

- **Names** come from every Contacts store on the Mac
  (`~/Library/Application Support/AddressBook/AddressBook-v22.abcddb` and one
  per account under `Sources/`), by the same rule as the phone's address
  book, so a card names a handle the same way from either source.
- **Attachments** that are on disk (`~/Library/Messages/Attachments/...`)
  become the observation's `media_path`. One not downloaded — kept in iCloud
  by *Optimize Mac Storage* — is still described in `value_json`; the report
  counts both.
- **The account** is the phone number the Mac sends from most, even when the
  Apple ID email sends more: a count that tipped between the two from one
  import to the next would move the mirroring scope with it. The report lists
  every handle the Mac sends from (`also sent from:`), and names the Mac when
  it read the Mac's own database.

## Full Disk Access (macOS)

macOS guards `~/Library/Messages`, `~/Library/Application Support/MobileSync`
(Finder backups), `AddressBook` (Contacts) and `CallHistoryDB` with its
privacy system. A process without permission gets *Operation not permitted*,
or from SQLite *unable to open database file* / *authorization denied* —
which looks like a broken file. The connectors report it as what it is, in one
line:

```
macOS did not allow reading /Users/alex/Library/Messages/chat.db (unable to open database file) — give the app you run this from Full Disk Access (…)
```

Grant it once: **System Settings > Privacy & Security > Full Disk Access**,
click **+**, add the app you run the command from (Terminal, iTerm, or your
editor), then quit and reopen that app. A scheduled run (launchd) needs the
same for the venv's `python`. Contacts that cannot be listed do not stop an
import — the messages come in, and the report says the names could not be
read.

## Which to use when — and both at once

- **Mac + iPhone:** `mac_messages`, every day. No cable, no backup, and the
  Mac's attachments link to real files. Use an iPhone backup through
  `ios_messages` too when the phone holds older history than iCloud keeps on
  the Mac, or before turning Messages in iCloud on.
- **Windows + iPhone:** `ios_messages` from an Apple Devices backup; there is
  no other way in ([windows-iphone.md](windows-iphone.md) has the setup).

Both can be imported into one database, in either order, as often as you
like:

- **One message is one record.** iMessage GUIDs are the same on every
  device, so the second import finds the first one's rows *identical*. The
  attachment path in `value_json` is kept relative to the Messages folder
  (`Attachments/ab/11/<guid>/IMG_0001.HEIC`) for exactly this reason — the
  phone says `Library/SMS/`, the Mac `Library/Messages/`.
- The one thing the two can honestly disagree on is **where a photo's file
  is**: the Mac knows its path, the phone's backup does not. Whichever
  source recorded the message first keeps its `media_path`; the report folds
  those into one line (`N message(s) already archived from another device
  differ only in where the attachment file is`) rather than a conflict per
  photo. Import from the Mac first and the files are linked. This is not a
  format upgrade (below): the phone's copy never knew the file, so nothing
  stored can be rewritten into the Mac's path — two devices know different
  things, and the first recorded stays.
- **Neither can delete the other's history.** Each connector mirrors only
  rows *it* first brought in, so an iPhone backup missing this morning's
  texts cannot remove what the Mac imported, and a Mac whose iCloud history
  starts later than the phone's cannot remove the phone's old years. A
  message deleted everywhere disappears through whichever source imported it.

## What comes in

One observation per message, keyed on the message GUID:

| stream | what |
|---|---|
| `imessage`, `sms`, `rcs` | the message, by the service that carried it |
| `text_reaction` | a tapback added or removed — loved, liked, laughed, a custom emoji, a sticker |
| `text_event` | something that happened to the thread: a member added or removed, someone changing their number, a rename (the new name is kept), someone leaving, the icon or background changed, a location share started or stopped, an audio message kept, SharePlay |

A thread event names who did it and — for membership changes — who it was done
to, not everyone in the room. An event type not yet mapped is counted under
"not understood" rather than imported half-understood.

- **Body.** Most text on current iOS is not in `message.text` but in
  `attributedBody`, an archived NSAttributedString. `connectors/typedstream.py`
  decodes it structurally; a body it cannot decode is counted in the report,
  never replaced with a guess.
- **Tapbacks are their own stream, with no body.** iOS stores a tapback as a
  row reading `Loved "see you at 6"`; kept out of the message streams, it
  cannot pollute message search or "last talked to".
- **value_json** carries the chat id, group flag, service, reply target,
  edit time and version history, attachment names/types/paths, audio flag,
  send effect, and app (for app messages) — only the fields that apply.
- **Attachments** are described, not copied: name, type, and the path
  relative to the Messages folder. The file stays where it is — in the
  backup, or on the Mac, where `media_path` points at it; media never enters
  the database. Texts imported by a build before 2026-10-22 kept the path
  as the database wrote it (`Library/SMS/Attachments/…` from a phone,
  `/var/mobile/Library/SMS/…` from an old one, `Library/Messages/…` from a
  Mac's `chat.db`). The next import that meets such a message — from
  either connector — rewrites the stored path to the current form in place:
  a **format upgrade**, not a conflict and not an update, since nothing the
  message says changed. The report says `N archived with the old full
  attachment path, rewritten to the current form (content unchanged)`, the
  import's counts carry `format_upgraded`, and the audit log gets one
  `format_upgrade` row per run naming the message ids (never content). A
  stored message is rewritten only when its upgraded `value_json` is exactly
  what the reader sends today; one whose `value_json` also differs in
  anything else stays as stored and is reported as a conflict, as before. A
  Mac's `media_path` beside an upgraded path is still the one folded line
  above. (Until 2026-10-22's fix those messages reported one
  `CONFLICT … (value_json)` each, on every import.) The mechanism is shared
  by every connector — docs/observed-tier.md, "Stored-format upgrades".
- **Participants are handles**: phone numbers (normalized — see below), email
  addresses, and, for Business Chat and alphanumeric senders like `AMAZON`, an
  `app_account` handle so they can be linked or ignored.
- **Your own number/address** appears as a handle labelled `you (this phone)`.
  Link it to your own person entity.

## Names: contacts suggest, you decide

The backup's own address book (`AddressBook.sqlitedb`: `ABPerson` names
joined to `ABMultiValue` numbers and addresses) — or, for `mac_messages`,
every Contacts store on the Mac — gives each handle its `display_name`: what
your phone calls that number ("First Last", else the nickname, else the
company). It never links a handle to an entity. A number on two cards keeps
both names (`Dad / Mom`), across stores too.

```
.venv\Scripts\python.exe -m lifecrm handles list --suggest
```

matches each unresolved handle's name against your entities and aliases —
exact, alias, then fuzzy: reordered names (`Reilly, Jake`), longer first names
(`Alex`/`Alexander`), initials (`Dana W.`) and accents (`José`/`Jose`) all
score — and prints the `handles link` command for each candidate. Nothing is
linked until you run one. Linking one handle attributes every message that
ever involved it, past and future.

## The archive mirrors the phone (decided 2026-09-24)

Deleted and unsent messages do not stay in the archive.

- **Deleted on the phone:** absent from the next backup, so its observation
  is removed — the row, its participants and its search-index entry. The
  dry-run report shows what an `--apply` would remove, by stream and by chat.
- **Unsent:** never imported; removed if an earlier import already has it.
- **In Recently Deleted** (iOS 16+ / macOS 13+): deleted. Messages keeps the
  row for about 30 days so it can be recovered, but moves its chat link from
  `chat_message_join` to `chat_recoverable_message_join`; the reader treats
  such a message as gone — not imported, not reported present — and the
  report counts it (`skipped: N × deleted message waiting in Recently
  Deleted`). A row still linked to its conversation as well counts as present.
- **Recovered on the phone** (Recently Deleted): back on the next import.
- **Aged out** (Settings › Messages › *Keep Messages*: 30 days or 1 year, which
  Messages in iCloud carries to the Mac): **kept.** The phone empties its
  oldest end by itself; that is not a deletion. Only a message missing from
  *inside* the span the source still holds — newer than its oldest message —
  is removed; anything older stays, and so do the days its import declared
  complete. The report says how many it kept (`kept: N older than the oldest
  message the source still holds`). The price: deleting your very oldest
  conversation reads the same as an age-out, so it stays too.
- **Edited:** re-imported with the current text and the edit time as
  `last_modified_at`, so it updates through the normal newer-wins path;
  `value_json.versions` carries the version history from
  `message_summary_info`.

What removal can never do:

- **Remove a message the connector merely failed to read.** Removal keys on
  what the phone's database still *holds*, not on what was imported this time
  — an undecodable body is counted in the report and the message stays.
- **Reach another phone's archive.** Only rows first imported from the same
  account (the phone's own number) are candidates, and a database that does
  not say whose phone it is removes nothing.
- **Destroy a journal entry's provenance.** A message a confirmed entry (or a
  relationship) cites is hidden (`status='retracted'`) instead of deleted,
  and comes back by itself — the same row, active again, reported as
  `back: N hidden record(s) the source holds again` — when a later backup of
  the same phone holds it again (recovered from Recently Deleted). `doctor`
  warns (`cited_observations_hidden`) while an entry or relationship cites a
  hidden message.
- **Take most of the archive at once by accident.** A partial backup —
  Messages in iCloud with *Optimize Storage* on keeps only recent texts on the
  phone — looks exactly like mass deletion. Removing half or more of the
  phone's archive (one record alone never counts), or five or more records
  that are over a tenth of it, in one import is refused unless you add
  `--allow-mass-removal`; the dry run warns before you get there.
- **Happen without an undo.** Any import that removes something first takes a
  database snapshot, `backups\pre-removal-<time>.db`, and prints the undo:
  `lifecrm restore "<that snapshot>"` (the whole database as it was just
  before the import). `imports retract` is *not* the undo for a removal — it
  hides what an import added, and says so when the import also removed
  something. Pre-removal snapshots are not pruned with the daily backups (a
  backup later that day would be the day's newest and push this one out);
  `backup` keeps every one from the 14 most recent days that have one.

Residue worth knowing about: snapshots hold a removed message's text until
retention prunes them, and the audit log — append-only by design — keeps the
old and new text of any *edit* that message ever had. The audit row for a
removal itself records only message ids, never text.

## Phone numbers

`+` and digits when the source has a `+`. Otherwise North American numbers are
recognised by shape — 10 digits with an area code starting 2–9, or 11 digits
starting with 1 — and given `+1`. Anything else stays bare digits: a
national-format number from another country cannot be completed without
knowing the country, and guessing could merge two people.

## Known limitations, by design

- Journal dates use the configured home timezone; the database records no
  zone per message. Texts sent while travelling land on the home-time day.
- An Android phone's reactions arrive over SMS as ordinary text and are
  imported as the messages they are. Recognising `Loved "…"` in a body would
  mean reinterpreting what someone wrote — the kind of derived guess the
  observed tier keeps out of stored records.
- Messages kept only in iCloud (Messages in iCloud with storage optimization)
  may be missing from a local backup.

## Freshness

`lifecrm doctor` warns (`ios_messages_data_age`) when the newest imported text
is more than 14 days old. It measures the newest *message*, not the newest
import run — re-importing an old backup does not make the data fresh. The two
connectors share their streams and doctor measures freshness per stream, so
the one check covers texts from either source; `mac_messages` declares no
second check that would only repeat it.

## First-real-import checklist (unproven until a real backup)

Everything above is tested against a synthetic backup built with the real
schemas and layout. What only a real phone can settle, the dry-run report now
states outright — the first run answers this list; note the answers here:

| check | the report line that answers it |
|---|---|
| [ ] Which backup folder the installed app actually uses | `read from: …` |
| [ ] The "could not be decoded" count — near zero, or a real share? | `skipped: N × message whose attributedBody could not be decoded`, and `not understood: … kept without its text` |
| [ ] Association types, services or thread events not yet mapped | `not understood: …` |
| [ ] Thread-event volume | `text_event: N new` |
| [ ] Whether the backup holds uncheckpointed writes | `sms.db-wal: read …` or `none` |
| [ ] Handle normalization against the phone's contacts | `normalized, e.g.: 5125559876 -> +15125559876 (Dana Whitfield)` — five, raw to stored |
| [ ] The oldest message against what the phone shows | `messages span …` |
| [ ] Mac: Recently Deleted really is `chat_recoverable_message_join` | delete a test message, re-run: `skipped: 1 × deleted message waiting in Recently Deleted` |
| [ ] Mac + phone: the same message really is identical from both | import both; the second should show no `CONFLICT` lines — if they name `value_json`, the chat ids differ between devices (newer versions prefix some with `any;`) |
| [ ] Mac: how many attachments are on disk vs in iCloud | `attachments: N on this Mac …, M not downloaded` |
| [ ] Mac: which handles it sends from, and the account picked | `account: …`, `also sent from: …` |

Answers so far: none — no real backup or Mac has been read. The owner's own
phone is Android; the first real run will be on an iPhone owner's own machine.

The Recently Deleted rule is read from the schema as Messages has written it
since iOS 16 / macOS 13 (`chat_recoverable_message_join (chat_id, message_id,
delete_date, …)`), not yet from a real database: the checklist row above is
the confirmation.

## Credits

The Messages schema map and its quirks (dual-epoch dates, chat-identifier
variants, attachment filtering) were cross-checked against
[mac_messages_mcp](https://github.com/carterlasalle/mac_messages_mcp) (MIT).
The typedstream layout was cross-checked against
[crabstep](https://github.com/ReagentX/crabstep), the parser behind
imessage-exporter (GPL-3.0); no code was taken from either, and crabstep's
real-world test blobs were used only to validate the decoder locally (74 of 75
parse fully with the right text; the 75th is truncated on purpose and is
correctly refused).

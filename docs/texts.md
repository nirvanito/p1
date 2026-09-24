# Texts from an iPhone (iMessage, SMS, RCS)

The iPhone has no API for its messages. What it does have is a local backup,
and an **unencrypted** local backup stores the Messages database as a plain
SQLite file. The `ios_messages` connector reads that file into the observed
tier. It never talks to the phone, never reads a live database in place, and
never sends anything.

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

## Importing

Dry-run first; it is the review step:

```
.venv\Scripts\python.exe -m lifecrm observe ios_messages
```

With no path, it looks where Apple's tools write backups —
`%USERPROFILE%\Apple\MobileSync\Backup` (Apple Devices), then
`%APPDATA%\Apple Computer\MobileSync\Backup` (desktop iTunes), then a Mac's
`~/Library/Application Support/MobileSync/Backup` — and uses the backup it
finds there. If there are several (two phones, or an old one), it lists them
with device name and date and asks you to pass one; it never guesses which
phone you meant. A path works too: the `<device id>` folder that holds
`Manifest.db`, or the `Backup` folder above it when that holds just one.

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
- **Attachments** are described, not copied: name, type, and the path inside
  the phone. The file stays in the backup; media never enters the database.
- **Participants are handles**: phone numbers (normalized — see below), email
  addresses, and, for Business Chat and alphanumeric senders like `AMAZON`, an
  `app_account` handle so they can be linked or ignored.
- **Your own number/address** appears as a handle labelled `you (this phone)`.
  Link it to your own person entity.

## Names: contacts suggest, you decide

The backup's own address book (`AddressBook.sqlitedb`: `ABPerson` names
joined to `ABMultiValue` numbers and addresses) gives each handle its
`display_name` — what your phone calls that number. It never links a handle to
an entity. A number on two cards keeps both names (`Dad / Mom`).

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
- **Recovered on the phone** (Recently Deleted): back on the next import.
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
- **Destroy a journal entry's provenance.** A message a confirmed entry cites
  is hidden (`status='retracted'`) instead of deleted.
- **Take most of the archive at once by accident.** A partial backup —
  Messages in iCloud with *Optimize Storage* on keeps only recent texts on the
  phone — looks exactly like mass deletion. Removing more than 100 records
  *and* more than 10% of the phone's archive in one import is refused unless
  you add `--allow-mass-removal`; the dry run warns before you get there.
- **Happen without an undo.** Any import that removes something first takes a
  standard database snapshot (`backups\lifecrm-<time>.db`, pruned by the usual
  tiered retention); `lifecrm restore` brings it back.

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
import run — re-importing an old backup does not make the data fresh.

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

Answers so far: none — no real backup has been read. The owner's own phone is
Android; the first real run will be on an iPhone owner's own machine.

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

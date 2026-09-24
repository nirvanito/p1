# Texts from an iPhone (iMessage, SMS, RCS)

The iPhone has no API for its messages. What it does have is a local backup,
and an **unencrypted** local backup stores the Messages database as a plain
SQLite file. The `ios_messages` connector reads that file into the observed
tier. It never talks to the phone, never reads a live database in place, and
never sends anything.

> **Read this first.** An unencrypted backup is your entire phone — messages,
> photos, notes, health data, saved Wi-Fi networks — sitting readable on disk.
> Importing texts also puts a decade of conversations into `lifecrm.db`, which
> is itself unencrypted. `docs/next-brief.md` open decision #1 (encryption at
> rest) exists for exactly this moment: settle it before the first real
> `--apply`. The connector is dry-run by default, so looking costs nothing.

## Making the backup (Windows)

1. Install **Apple Devices** from the Microsoft Store (or iTunes on older
   setups) and connect the iPhone by cable. Trust the computer on the phone.
2. Under *Backups*, choose **Back up all of the data on your iPhone to this
   computer**, and make sure **Encrypt local backup is unticked**. An encrypted
   backup cannot be read without its password; the connector says so and stops.
3. Back up now. The backup lands in one of:

   ```
   %USERPROFILE%\Apple\MobileSync\Backup\<device id>\
   %APPDATA%\Apple Computer\MobileSync\Backup\<device id>\
   ```

   The `<device id>` folder — the one holding `Manifest.db` — is the target.

## Importing

Dry-run first; it is the review step:

```
.venv\Scripts\python.exe -m lifecrm observe ios_messages "C:\...\Backup\<device id>"
```

The report names the device and backup date, then shows per stream how many
records are new or changed, the date span, and the latest one as a sample. It
lists what was skipped and what was not understood, with counts. **Read the
samples**: garbled text, a wrong year, or a large "could not be decoded" count
means stop and look. When it looks right, add `--apply`.

Re-running on the same backup is a no-op; a later backup adds only what is new.

Also accepted: a folder holding `sms.db` (or a Mac's `chat.db`) with an
optional `AddressBook.sqlitedb` beside it, or a bare `sms.db`/`chat.db` file.
A bare file is copied through SQLite's backup API, never opened in place.

## What comes in

One observation per message, keyed on the message GUID:

| stream | what |
|---|---|
| `imessage`, `sms`, `rcs` | the message, by the service that carried it |
| `text_reaction` | a tapback added or removed — loved, liked, laughed, a custom emoji, a sticker |
| `text_unsend` | a message its sender unsent |

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

## The archive remembers (open decision #2, as implemented)

- **Deleted on the phone:** absent from the next backup; the observation stays.
- **Unsent:** the row is still there with its text cleared. It is *not*
  re-imported as a message — that would blank the body the archive already
  holds — and is recorded as its own `text_unsend` observation.
- **Edited:** re-imported with the current text and the edit time as
  `last_modified_at`, so it updates through the normal newer-wins path; the
  audit log keeps the previous body, and `value_json.versions` carries the
  full history from `message_summary_info`.

This is the provisional answer from `docs/observed-tier.md`. Confirm it before
the first real import — it is the recoverable direction, but it is a choice.

## Phone numbers

`+` and digits when the source has a `+`. Otherwise North American numbers are
recognised by shape — 10 digits with an area code starting 2–9, or 11 digits
starting with 1 — and given `+1`. Anything else stays bare digits: a
national-format number from another country cannot be completed without
knowing the country, and guessing could merge two people.

## Known limitations, by design

- Journal dates use the configured home timezone; the database records no
  zone per message. Texts sent while travelling land on the home-time day.
- Group events (member added/removed, renamed, photo changed) are counted, not
  imported, in this version.
- An Android phone's reactions arrive over SMS as ordinary text and are
  imported as the messages they are.
- Messages kept only in iCloud (Messages in iCloud with storage optimization)
  may be missing from a local backup.

## Freshness

`lifecrm doctor` warns (`ios_messages_data_age`) when the newest imported text
is more than 14 days old. It measures the newest *message*, not the newest
import run — re-importing an old backup does not make the data fresh.

## First-real-import checklist (unproven until a real backup)

Everything above is tested against a synthetic backup built with the real
schemas. These are the facts only a real phone can settle — note the answers
here:

- [ ] Which backup folder the installed app actually uses.
- [ ] The "could not be decoded" count: near zero, or a real share?
- [ ] "not understood" lines: association types or services not yet mapped.
- [ ] Group-event volume, and whether it is worth importing.
- [ ] Whether the backup holds `sms.db-wal` (uncheckpointed writes).
- [ ] Spot-check five handles' normalization against the contacts on the phone.
- [ ] The oldest message date against what the phone shows.

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

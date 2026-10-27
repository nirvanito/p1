# Texts, calls and contact names from Phone Link (Windows)

A Windows 11 laptop with a linked Android phone already holds the phone's
recent texts, its call log and its address book: Phone Link (the app behind
*Link to Windows*) keeps them in a cache of its own so it can show them. The
`phone_link` connector reads that cache. Nothing is installed on the phone,
nothing is scheduled, nothing is uploaded anywhere, and the morning job
collects it like any other source. For a Samsung this is the texts route
with the least to set up: Link to Windows is built in.

What it reads lands in the **observed tier** — bulk records that never pass
the `/log` review gate, are never asserted as fact, and are always cited
([observed-tier.md](observed-tier.md)) — in the very streams the SMS Backup
& Restore import writes (`android_sms`, `android_mms`, `android_call`,
[android-texts.md](android-texts.md)) plus `contact_card`
([contacts.md](contacts.md)), so `ask`, People, the vault and the daily
series need nothing new.

```
the phone ──Link to Windows──> Phone Link on the laptop
            %LOCALAPPDATA%\Packages\Microsoft.YourPhone_8wekyb3d8bbwe\LocalCache\Indexed\<device>\System\Database\
              phone.db      message, mms + mms_part, participant, subscription   -> android_sms, android_mms
              calling.db    call_history                                          -> android_call
              contacts.db   contact, phonenumber, emailaddress, ...               -> contact_card, and the names on handles
            ──lifecrm collect (06:30)──> observations
```

## The route, and why this one

| | Phone Link (this page) | SMS Backup & Restore ([android-texts.md](android-texts.md)) |
|---|---|---|
| on the phone | nothing: Link to Windows, signed in | an app, a scheduled backup, OneDrive or the phone in File Explorer |
| how much | a **window**: roughly the last month of texts, the call log Phone Link shows | the phone's **whole** text store and call log |
| deletions | never removed here (a text gone from the cache aged out of it) | texts mirror the phone (a deleted text leaves the archive) |
| names | the phone's address book, included | the backup's `contact_name` |
| contact cards | yes, mirrored | no |
| calls | only when Phone Link syncs calls (the phone paired over Bluetooth) | yes |
| when | whenever Phone Link has synced — continuously while the laptop is on | the nightly backup |

Both write the same streams, and an SMS or a call gets the **same id** from
either, so where they overlap they are one record. An MMS does not (below).
So: **one route for texts per phone.** Phone Link for a setup with nothing to
maintain; SMS Backup & Restore when you want the full history and mirrored
deletions. If you use both, an MMS from the overlapping month counts twice.
That is the same rule as a Mac's Messages beside an iPhone backup
([texts.md](texts.md#which-to-use-when--and-both-at-once)).

## Setting it up (once)

1. **On the phone:** Quick settings › **Link to Windows**, signed in with the
   Microsoft account the laptop uses. A Samsung has it built in; another
   Android phone installs *Link to Windows* from the Play Store.
2. **On the laptop:** Settings › Bluetooth & devices › **Mobile devices** (or
   the Phone Link app) › add the phone. Open Phone Link's **Messages** tab
   once, so it starts syncing texts and contacts. **Calls** need the phone
   paired over Bluetooth; without that there is no call log in the cache,
   and the import says so **(verify)** which Phone Link versions sync calls
   without Bluetooth.
3. That is all. The next morning `lifecrm collect` finds the cache and asks
   ([collect.md](collect.md)):

   > New on this computer: texts, calls and contact names (Phone Link).
   > Import it? 4,029 record(s) from 2026-09-27 to 2026-10-26, in …

   `lifecrm collect approve phone_link` shows the dry run and imports on
   your yes; from then on every morning brings in what is new. Or look now:

```
lifecrm observe phone_link                 # dry run: finds the cache by itself
lifecrm observe phone_link --apply
lifecrm handles list --suggest             # names from the phone's address book
```

The phone in File Explorer ([android.md](android.md#no-sync-app-at-all-windows-11-optional))
is a different thing: that is the phone's *files*, read for the recorder's
notes, SMS Backup & Restore's backups and GPSLogger's tracks. Phone Link's
cache is the laptop's own copy of the phone's *messages*; the two are
independent and work side by side.

## Importing

```
phone-link: d4d763e2/phone.db + calling.db + contacts.db (Phone Link's cache for the linked phone, folder d4d763e2-…)
  read from: C:\Users\you\AppData\Local\Packages\Microsoft.YourPhone_8wekyb3d8bbwe\LocalCache\Indexed\d4d763e2-…\System\Database
  texts: 4029 message row(s) read (SMS 177, MMS 3852), 2026-09-27..2026-10-26 — a window of what the phone holds; remembered, never removed here
  calls: none in the cache — Phone Link syncs calls only with the phone paired over Bluetooth (its Calls tab); texts and contacts come in regardless
  contacts: 138 card(s), the phone's address book — a contact deleted on the phone is removed here
  account: +15125550100 (from Phone Link's subscription row)
  normalized, e.g.: 5125550142 -> +15125550142 (Jake Reilly)
  android_mms: 3851 new, 0 changed | 2026-09-27..2026-10-26 | e.g. 2026-10-26 them: who's in for saturday
  android_sms: 177 new, 0 changed | 2026-09-27..2026-10-26 | e.g. 2026-10-26 you: perfect, bringing snacks
  contact_card: 138 new, 0 changed | 2025-11-06..2026-10-21 | e.g. 2026-10-21 card: Jake Reilly
  1 duplicate(s) inside the cache
  handles: 159 (136 named by the phone's contacts) — review with 'lifecrm handles list --suggest'
  one route for texts: an MMS this cache holds and a SMS Backup & Restore backup of the same phone count twice (docs/phone_link.md)
dry run — nothing written. Re-run with --apply to write.
```

Read the samples against what the phone shows (`you` is a text you sent),
then `--apply`. Re-running is a no-op for what is already stored: records
dedupe on their ids, and an unchanged cache is "nothing new".

With no path the connector finds the cache itself (below). A path can be the
device folder under `Indexed\`, its `System\Database` folder, or a copied
`phone.db` (the `calling.db` and `contacts.db` beside it are read when
present).

## What comes in

| stream | one record per | fields |
|---|---|---|
| `android_sms` | SMS (`message`) | direction (`in`/`out`), body, the other party as a `phone` handle (or `app_account` `messages:AMAZON` for a sender id), `value_json.thread` |
| `android_mms` | MMS (`mms` + its `mms_part` rows) | body (the text parts joined), `attachments` (name and MIME type only — the media bytes are never read), `group` for a group thread, `subject`; every member of the thread as a participant |
| `android_call` | call (`call_history`) | direction, `value_json.call` (`in`, `out`, `missed`, `voicemail`, `rejected`, `answered_elsewhere`), duration in seconds, `ended_at` |
| `contact_card` | contact (`contact` + its numbers, emails, addresses, dates, URLs) | title = the display name; `value_json` phones, emails, addresses, urls, birthday, org, title, nickname; every number and email as a participant |

Your own number is attached to every text and call as `you (this phone)`,
the label the SMS Backup & Restore and iPhone imports use, so review tools
treat every phone's owner alike. Times are the phone's own, kept to the
millisecond (an MMS to the second, as Android stores it); a text at 00:30
belongs to the previous journal day, as everywhere else.

An **RCS chat** (Google Messages' chat features) sits in a table of its own,
`rcs_chat`, and is read as `android_mms` with `value_json.app = "rcs"`. The
cache this was written from held no RCS rows, so that part is **unverified**:
the report says when it read any, and the samples show the direction to
check.

## Identity

- An **SMS** is `android:` + a SHA-1 of the thread's normalized number, the
  time to the millisecond, the direction and the text — the id
  [android-texts.md](android-texts.md#identity) gives the same message from a
  backup. A **call** likewise (number and time). So an import from either
  route is a no-op for what the other stored.
- An **MMS** from a backup carries the carrier's message id; the cache does
  not, so here an MMS hashes its thread, time, direction, text and
  attachment names. Across the two routes the ids differ: the "one route"
  rule above.
- A **contact card** is `phonelink:<cache folder id>:<contact id>` — stable
  from one morning to the next, and scoped to this phone, so a second phone's
  cards never collide.

## Whose phone

The owner's number scopes mirroring and labels the owner handle. In order:

1. Phone Link's `subscription` row — the phone's own number, which the
   cache names on every phone seen so far;
2. the `from` of the phone's own sent MMS;
3. `account` under `[connectors.phone_link]` in `config.toml`.

With none, the report says `account: unknown`, nothing is ever removed, and
no owner handle is attached (set `account` and the next import fills it).

## Texts and calls are remembered; contacts mirror

Phone Link keeps a **window** (about a month of messages was observed) and
drops older rows itself. A text missing from the cache is not evidence it
was deleted, so nothing on the three text and call streams is ever removed
by this connector, and no day is declared complete (a missing row is
unknown, never zero: [assistant.md](assistant.md#coverage-what-a-missing-row-means)).
The archive grows from day to day: a year of mornings holds a year of texts,
each read within its month.

`contacts.db` is the phone's whole address book, so **contact cards
mirror**: a contact deleted on the phone leaves the archive at the next
import, scoped to this connector and this phone, with the usual guard (a
removal of a large share at once is refused and held for your yes:
[collect.md](collect.md)). A card a journal entry or a relationship cites is
hidden, not deleted.

## Names: contacts suggest, you decide

Every number and email in `contacts.db` carries the card's name, which
labels the same handle wherever it appears in texts and calls; a number on
two cards keeps both names ("Dad / Mom"). They are display names only.
Nothing is linked to a person until you say so:

```
lifecrm handles list --suggest
lifecrm handles link phone:+15125550142 --entity <id>      # or --new "Jake Reilly"
```

If Google Contacts or a `.vcf` is already your contacts source, the same
people would appear twice (different ids per source). Leave the cards out
and keep the names:

```toml
[connectors.phone_link]
contacts = false            # names still label handles; no contact_card records
```

## Two phones

Phone Link keeps one cache folder per phone ever linked, and a replaced
phone's folder stays behind. With no path, the connector takes the one
folder whose `phone.db` was written in the last 14 days; a folder older than
that is a phone no longer linked and is left alone (import it by hand with
its path if you want its month). Two folders both written in the last 14
days are two linked phones: the import refuses to guess, names both, and
asks for `[connectors.phone_link] path`.

## Freshness

`lifecrm doctor` warns (`phone_link_data_age`) and the morning nudges say so
when the newest record is over 14 days old — Phone Link stopped syncing (the
phone unlinked, the app not running, Bluetooth off). The check starts once
this connector has an import of its own.

## Settings

```toml
[connectors.phone_link]
# path = '~\AppData\Local\Packages\Microsoft.YourPhone_8wekyb3d8bbwe\LocalCache\Indexed\<device>'   # two live phones
# account = "+15125550100"    # the phone's own number, when the cache does not say
# contacts = true             # false: names only (see above)
```

## Known limitations, by design

- **A month, not the history.** The window is Phone Link's; the first
  import brings what the cache holds now, and history before it comes from a
  SMS Backup & Restore backup — once, by hand, accepting that its MMS of the
  overlapping month count twice.
- **Calls need Bluetooth.** Phone Link shows (and caches) calls only with the
  phone paired; otherwise there is no call log here, and the report says so.
- **Attachments as names.** A photo sent in a text is recorded by file name
  and type; its bytes stay in the cache and are never read.
- **An undocumented format.** The databases are Phone Link's own. Every
  column this reader needs is checked before anything is read; a Phone Link
  update that changes them is one line, `Phone Link's phone.db: message has
  no column(s) …`, never a silent misread. Report it with the version.
- **Windows only.** A Mac keeps no Phone Link cache; there the route is the
  Mac's Messages ([texts.md](texts.md)).
- **Phone Link must have synced.** It syncs while it (or its background
  service) runs and the phone is reachable; a laptop that was off for a week
  catches up when it is back.
- **Photos are not read here.** Phone Link also caches recent photos'
  metadata; those come in through the Gallery › OneDrive route instead
  ([photos.md](photos.md)), which has the whole roll.

## Verified against a real cache (2026-10)

The reader was written from, and run against, a real Phone Link cache on
Windows 11 (counts and shapes only were looked at, never message text):

- [x] the folder layout, the three databases with their `-wal` and `-shm`,
      and copying them while Phone Link runs;
- [x] every table and column named above; times as FILETIME at millisecond
      precision for SMS and calls, second precision for MMS;
- [x] `message.type` 1 / 2 and `mms.type` 1 / 2 as inbox / sent; a sent SMS
      names its recipient in `to_address` and no sender; a received MMS lists
      the other recipients in `to_addresses`, comma-separated;
- [x] `participant` rows name every thread's other members (the owner left
      out); `subscription.number` is the phone's own number, and a sent
      MMS's `from_address` is the same number;
- [x] `mms_part`: one `text/plain` part per message with text, a
      `application/smil` layout part (sequence -1), media parts with name
      and type;
- [x] the whole scan under a second for a month of texts and 138 cards.

Not yet seen on a real cache: a call log (`call_history` was empty — the
phone was not paired over Bluetooth), RCS rows, contact dates (birthdays),
email and postal fields, how far back the window reaches on other phones,
and whether a text deleted on the phone inside the window disappears from
the cache (it is not removed here either way). The first real import with
calls settles the first of these; the report's samples the rest.

## Commands

```
lifecrm observe phone_link [<cache folder | System\Database | phone.db>]
lifecrm observe phone_link --apply
lifecrm collect approve phone_link
lifecrm imports list
lifecrm imports retract <uuid>
lifecrm handles list --suggest
lifecrm where                              # the cache folder(s) Phone Link keeps, and when each was written
```

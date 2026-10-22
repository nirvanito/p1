# Texts and calls from an Android phone (SMS Backup & Restore)

Android has no export for texts. Samsung's Smart Switch writes an encrypted,
proprietary archive, Google Messages has no export, and Google Takeout does not
include SMS. The free **SMS Backup & Restore** app (SyncTech) is the standard
way out: on a schedule it writes the phone's SMS, MMS and call log as two XML
files, and it can put them straight into OneDrive. The `android_sms` connector
reads those files into the observed tier. It never talks to the phone and
never sends anything.

This is the texts half of the Windows laptop + Samsung setup. (An iPhone's
texts come from its backup instead — see `docs/texts.md`.)

## Making scheduled backups to OneDrive (on the phone)

Menu names move between app versions; what matters is the four choices in
bold.

1. Install **SMS Backup & Restore** (SyncTech) from the Play Store. Allow it
   SMS, phone/call logs and **contacts** — contacts are what put names on the
   numbers in the backup.
2. *Set up a backup*: tick **Messages** and **Call logs**. Keep MMS on — on
   Android a group text *is* an MMS, so without it the group threads vanish.
   Attachments (photos, videos) are optional: the connector never reads
   media, so leaving them out only makes the files smaller.
3. *Where*: choose **OneDrive** and sign in with the same Microsoft account as
   the laptop. The app makes its own folder there.
4. *Schedule*: **daily** (2 AM is fine). Do **not** turn on the option that
   backs up only new messages since the last backup (archive / incremental
   mode), and do not limit the backup to selected conversations. A backup
   that is not the whole phone cannot be mirrored (below).
5. *Back up now* once. On the laptop, the OneDrive folder should then hold
   `sms-<date><time>.xml` and `calls-<date><time>.xml`.

## Importing

Dry-run first; it is the review step:

```
.venv\Scripts\python.exe -m lifecrm observe android_sms
```

With no path it looks, in order, in `[connectors.android_sms] folder`, then
under each OneDrive root (`%OneDrive%`, or `~/Library/CloudStorage/OneDrive-*`
on a Mac) in `Apps\SMS Backup & Restore`, `Apps\SMS Backup and Restore`,
`Apps\SMSBackupRestore`, `SMSBackupRestore` and `SMS Backup & Restore`, then
in `~/Downloads` — and uses the first of those that holds a backup. When
nothing is found it says everywhere it looked. A path works too:

- a folder — the newest `sms-*.xml` and the newest `calls-*.xml` in it are
  read (newest by the time stamp in the name);
- a `.zip` holding them (same rule);
- one file. A file is recognised by what is inside it (`<smses>` or
  `<calls>`), not by its name.

The settings, all optional, go in `config.toml`:

```toml
[connectors.android_sms]
folder = "~/OneDrive/Apps/SMS Backup & Restore"   # where the backups land
account = "+15125550100"   # this phone's own number (see "Whose phone")
# mirror = true            # only if your files do not say type="full" (below)
```

The report names the files and folder it read and the app version that wrote
them, says for texts and calls whether each file is a full backup and when it
was taken, gives the phone's own number and how it was found, the span of the
messages, a few numbers as written vs. as stored, and per stream how many
records are new or changed with the latest as a sample. It lists what was
skipped and what was not understood, with counts. **Read the samples**: a wrong
year, garbled emoji or a large "not understood" count means stop and look.
When it looks right, add `--apply`. `--json` carries the same facts for
scripts.

Re-running on the same backup is a no-op ("already imported" is noted); a
later backup adds only what is new.

## What comes in

| stream | what |
|---|---|
| `android_sms` | a text message |
| `android_mms` | an MMS — group texts, pictures, long texts: who sent it, everyone it went to, its text, and its attachments *described* |
| `android_call` | a call: `value` = seconds, `unit` = `s`, `ended_at` = start + duration |

- **value_json** — texts: `thread` (the other party's handle, or for a group
  every member's, sorted and comma-joined — the same key for SMS and MMS with
  the same people), `group`, `attachments` (`name`, `mime`), `subject`, `app`
  (the messaging app that wrote the MMS). Calls: `call` = `in`, `out`,
  `missed`, `voicemail`, `rejected` or `answered_elsewhere`, and `number` =
  `private` / `unknown` / `payphone` when the network withheld it.
- **Attachments are described, never copied.** The backup carries media
  inline as base64; it is never decoded, and media never enters the database.
- **Participants are handles**: phone numbers (normalized), email addresses,
  and an `app_account` handle for alphanumeric senders like `AMAZON`, so they
  can be linked or ignored. Roles are `sender` / `recipient` — for a call, the
  caller and the called.
- **Your own number** appears as a handle labelled `you (this phone)`. Link
  it to your own person entity.
- **Not imported, counted in the report:** drafts, messages still in the
  outbox or failed (never delivered), MMS that were never downloaded (only
  their notification is on the phone), and blocked calls (almost all spam;
  each would add a number to the handle review queue).
- **Emoji.** The app writes each emoji as two UTF-16 surrogate-pair
  references (`&#55357;&#56832;`), which no XML parser accepts. The connector
  repairs them as it reads — the file on disk is untouched — and the report
  counts the repairs.
- `readable_date` is the phone's locale rendering of the time and is never
  parsed; `date` (milliseconds since 1970) is the time.

## Names: contacts suggest, you decide

Each message and call carries `contact_name` — what the phone called that
number on the day of the backup (`(Unknown)` for a stranger). It becomes the
handle's `display_name` and nothing more; it never links a handle to an
entity. A group MMS lists its members' names comma-separated in the same
order as their numbers; when a name itself contains a comma the two no longer
line up, and those numbers are left unnamed rather than misnamed (the report
counts them).

```
.venv\Scripts\python.exe -m lifecrm handles list --suggest
```

offers entity candidates for each unresolved handle by name; nothing is
linked until you run the `handles link` it prints.

## Identity

The backup has no message id — the phone's own row ids are not exported and
would not survive a restore anyway. A message's id is its content: `android:`
plus a SHA-1 of the thread's normalized number(s), the time to the
millisecond, the direction and the text. An MMS uses the carrier's message id
(`m_id`) with its time instead, when it has one. So:

- a re-import, a later backup, or the same backup after the app re-spells a
  number (`(512) 555-0142` vs `+15125550142`) is a no-op for what is stored;
- a message still in the outbox at one backup keeps its id once it is sent;
- two genuinely identical texts in the same millisecond are one record (the
  report counts "duplicate(s) inside the backup").

## Whose phone

Mirroring (next section) is scoped to the phone's own number, so the import
has to know it. In order:

1. `account` under `[connectors.android_sms]` in `config.toml`;
2. the `from` of the phone's own sent MMS (any group text you sent);
3. the one number every received MMS was addressed to.

If none of these says, the report says `account: unknown` and nothing is ever
removed. Set `account` and the next import mirrors.

## Texts: the archive mirrors the phone (decided 2026-09-24)

A text deleted on the phone is removed by the next import — but only from a
file that is the phone's **whole** state:

| the file says | mirrors? |
|---|---|
| `type="full"` on its root | yes |
| any other type (an incremental / only-new backup) | never |
| no type at all (older app versions) | only with `mirror = true` in `[connectors.android_sms]` |
| a `count` that disagrees with what it holds | never — the file is incomplete |
| anything, with `mirror = false` | never |

Every id in a mirroring file counts as present, including drafts and the
other rows this connector skips.

**Calls are remembered, never removed** (decided 2026-10-22). A
`calls-*.xml`, even a `type="full"` one, is not the phone's whole call
history: Android's call log keeps only its newest calls (500 on stock
Android; Samsung sets its own limit) and drops the oldest by itself, a few a
day. Mirroring it removed every call the phone aged out — a handful per
import, never enough to trip the mass-removal guard — until the archive held
no more than the phone does, which defeats keeping years of calls. So a call
missing from a later backup stays archived, whether the phone dropped it or
you deleted it from Recents; the report says so on the `calls:` line (`kept
even once the phone's call log drops them`). `lifecrm imports retract`
still undoes a whole import. (Mirroring only within the span a calls file
covers — removing a missing call only when it is newer than the file's
oldest call — would keep deletions *and* history; it needs the shared write
layer to scope removal by time, and is not built.)

What removal can never do — the same guarantees as the iPhone (`docs/texts.md`):

- **Reach another phone's archive.** Only rows first imported under the same
  account (number) are candidates.
- **Destroy a journal entry's provenance.** A text a confirmed entry cites is
  hidden (`status='retracted'`) instead of deleted.
- **Take most of the archive at once by accident.** Removing half or more of
  that phone's archive, or five or more records that are over a tenth of it,
  in one import is refused without `--allow-mass-removal`; the dry run warns
  first.
- **Happen without an undo.** An import that removes anything first takes a
  database snapshot (`backups\pre-removal-<time>.db`) and prints the undo:
  `lifecrm restore "<that snapshot>"`. `imports retract` cannot bring back
  what an import removed.

**Changing what the backup includes looks like deleting it.** Turning MMS
off, or limiting the backup to some conversations, makes the next full
backup lack those rows. A large change is refused by the guard above; a
small one shows in the dry run as `remove: N no longer on the phone` — read it
before `--apply`, or set `mirror = false` while you experiment.

## Phone numbers

The same rule as the iPhone connector: `+` and digits when the source has a
`+`; 10-digit North American numbers (area code 2–9) and 11-digit numbers
starting with 1 get `+1`; anything else stays bare digits — a national number
from another country cannot be completed without knowing the country.

## Known limitations, by design

- **Journal dates use the configured home timezone**; the backup records no
  zone per message. Texts sent while travelling land on the home-time day.
- **RCS — unverified.** Samsung Messages keeps its own RCS ("chat") messages
  outside the shared SMS/MMS store, so SMS Backup & Restore may not see them
  at all. Google Messages is reported to file RCS into the MMS store; those
  import as `android_mms`, and the report counts MMS carrying the `proto:`
  transaction id that is said to mark them. Check this on the real phone
  (checklist).
- **Android group texts have no names**; the thread is its member list.
- **Reactions from an iPhone** arrive as ordinary text (`Loved "…"`) and are
  imported as the messages they are.
- A message whose whole text is the word `null` reads as empty: the app
  writes absent values as the literal string `null`.
- `contact_name` is a snapshot from the backup day, and a handle's first
  name sticks (the shared write layer never overwrites a display name).
- Dual-SIM phones: which SIM carried a message is not recorded; the account
  is one number.

## Freshness

`lifecrm doctor` warns (`android_sms_data_age`) when the newest imported text
or call is more than 14 days old — the sign that the scheduled backup or the
OneDrive upload stopped. It measures the newest *record*, not the newest
import run.

## First-real-import checklist (unproven until a real backup)

Everything above is tested against synthetic files written in the app's
format as documented and remembered, not against a real phone's backup. The
dry-run report states what only a real backup can settle — note the answers
here:

| check | the report line that answers it |
|---|---|
| [ ] Which OneDrive folder the app really uploads to | `read from: …` (add it to `folder` if it was not found) |
| [ ] Whether the root says `type="full"` on a scheduled backup | `texts: full backup taken …` / `type not stated` |
| [ ] Whether `count` matches what the file holds (does it count MMS too?) | `N message(s) (file says M)` appears only on a mismatch |
| [ ] Whether a *selected-conversations* backup still says `full` (if so, never use that option with mirroring) | `texts: … backup` on such a file |
| [ ] Whether the account is found without config | `account: … (from …)` |
| [ ] MMS date units (milliseconds expected) | `--json` → `facts.mms_date_units` |
| [ ] Which app wrote the MMS, and whether RCS shows up | `--json` → `facts.mms_apps`; `rcs: N MMS carry a 'proto:' …` |
| [ ] Emoji repaired, not mangled | `repaired: N emoji …` and the samples |
| [ ] Group names line up with numbers | `N group MMS whose contact names did not line up` |
| [ ] Handle normalization against the phone's contacts | `normalized, e.g.: …` |
| [ ] The oldest message against what the phone shows | `messages span …` |

Answers so far: none — no real backup has been read yet.

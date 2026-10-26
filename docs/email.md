# Email (Gmail, Apple Mail, Thunderbird, .eml)

Mail is where advice, plans and confirmations get written down at length.
"What exactly did Priya tell me about the deck?" is answered by an email
more often than by a text. The `email` connector reads the mail archives
you already have into the observed tier: a Google Takeout of Gmail, an
Apple Mail export or Apple Mail's own store on a Mac, a Thunderbird profile,
or saved `.eml` files. It never logs in to a mail server, never calls an
API, and never sends anything.

Like texts and calendars, mail lands in the **observed tier**: bulk records
that never pass the `/log` review gate, are never asserted as fact, and are
always cited. Nothing here can touch `entries`, `measurements` or
`mentions`. See [observed-tier.md](observed-tier.md) for why.

## Getting the archive

**Gmail, every morning, with no export.** Connect it once —
`python -m lifecrm connect gmail you@gmail.com` with an app password — and
the daily `collect` fetches new mail over IMAP (read-only) into `.eml`
files this connector reads, older mail backfilled a batch a morning. See
[gmail.md](gmail.md). The Takeout below is the one-off alternative.

**Gmail, on either setup (Windows + Samsung, or Mac + iPhone).** Go to
[takeout.google.com](https://takeout.google.com) → **Deselect all** → tick
**Mail** (its *All Mail data included* button picks labels; leave it at all
mail) → **Next step** → *Export once*, *.zip*, the largest file size offered
→ **Create export**. Google emails you when it is ready, which for a large
mailbox can take hours or days; download it. You get
`takeout-<date>T<time>Z-001.zip` holding
`Takeout/Mail/All mail Including Spam and Trash.mbox`. Leave it zipped: the
connector reads the mbox straight out of the zip. (Takeout's menus change
from time to time; the names above are how they read in 2026. The folder is
called `Mail` in an English-language Google account — another language may
name it differently, in which case pass the zip's path or set `path`.)

**Apple Mail on the Mac.** Two ways:

- *Export*: select one or more mailboxes in Mail's sidebar (⌘-click for
  several) → **Mailbox** menu → **Export Mailbox…** → choose a folder. Mail
  writes one `<Mailbox>.mbox` folder per mailbox, each holding an `mbox`
  file and a `table_of_contents`. Pass the folder that holds them.
- *The store itself*: `~/Library/Mail` (`V10/` and friends inside) — every
  account Mail syncs, with nothing to export. macOS guards it: the app you
  run this from needs **Full Disk Access** (System Settings → Privacy &
  Security → Full Disk Access: add Terminal, iTerm or your editor, then quit
  and reopen it). Without it the import stops with one line saying so.

**The phones.** Neither the iPhone's Mail app nor the Samsung Email app can
export anything: they are windows onto an account (iCloud, Gmail,
Exchange). Export the account — Takeout for Gmail, Apple Mail on the Mac for
iCloud and the rest.

**Thunderbird.** Help → Troubleshooting Information → *Profile Folder* →
Open Folder. `Mail/Local Folders/` and `ImapMail/<server>/` hold one mbox
file per folder (`Inbox`, `Sent`, … — no extension, beside an `Inbox.msf`
index). Pass `Mail` or `ImapMail/<server>`. An IMAP folder holds only what
Thunderbird has downloaded for offline use.

**Anything else.** Messages saved as `.eml` (drag them out of most mail
apps into a folder) are read one file per message. Outlook's own `.msg` and
`.pst` files are not read.

## Tell it which address is you

Direction ("did I send this or receive it?") needs your addresses. Say them
once in `config.toml`:

```toml
[connectors.email]
addresses = ["alex.rivera@example.com", "alex@work.example"]
# path = "~/Downloads/takeout-20261020T120000Z-001.zip"   # what 'observe email' reads with no path
# since = 2020-01-01          # import a window of a very large archive
# include_lists = false       # true keeps newsletters and mailing lists
# skip_labels = ["Spam", "Trash", "Junk", "Bin", "Deleted Messages", "Deleted Items",
#                "Junk E-mail", "Junk Email", "Drafts", "Draft", "Chat"]
```

Mail fetched by the Gmail source ([gmail.md](gmail.md)) names the account
it came from (an `X-Lifecrm-Account` first line): that address is always
one of yours and the import's account, whatever else is set.

Without `addresses`, the email handles under `[owner]` (see
[ask.md](ask.md), "Who you are") are used. Without either, the dry run
**infers** you — the most frequent sender of the archive's sent mail (Gmail's
`Sent` label, a Sent mailbox), else the address mail was delivered to
(`Delivered-To`) — says so, and lists any other sender of sent mail ("also
sent from: …") that might be an alias of yours. When nothing says who you
are, direction stays unknown and the dry run prints a **WARNING**. Set it
before `--apply`: direction is recorded once, and changing it later is a
*conflict* (kept existing), not an update.

## Importing

Dry-run first — always. It is the review step.

```bash
.venv/Scripts/python -m lifecrm observe email takeout-20261022T120000Z-001.zip
```

```
email: takeout-20261022T120000Z-001.zip (zip, 16 message(s) read from 1 mail file(s))
  mailbox All mail Including Spam and Trash: 14 kept, 2 skipped
  you: alex.rivera@example.com ([owner] handles in config.toml)
  email: 14 new, 0 changed | 2022-08-17..2026-10-22 | e.g. 2026-10-22 from sam.okafor@example.com: New climbing gym Saturday?
  direction: 8 in, 5 out, 1 to yourself
  handles: 8 email address(es) — review with 'lifecrm handles list --suggest'
  attachments: 3 described (name, type, size — never the bytes); 1 calendar invitation(s) kept as email
  bodies: 6 trimmed of a quoted reply or a signature (value_json text_length keeps the whole text's length)
  skipped: 1 × mailing-list or bulk mail (List-Unsubscribe or Precedence: bulk; include_lists = true keeps it)
  skipped: 1 × message labelled Spam (skip_labels)
dry run — nothing written. Re-run with --apply to write.
```

Read the **span** (does the oldest date match the archive?), **you** (is it
your address, and where did it come from?), the **direction** split (all
"in" means you are not recognised), and the **skipped** lines. Then add
`--apply`; the import uuid it prints undoes the whole run with
`lifecrm imports retract <uuid>`. `--json` carries the same facts for
scripts.

Accepted targets:

- the Takeout `.zip`, or the folder it unzips to
- an `.mbox` file, the `mbox` inside an Apple Mail export, a Thunderbird
  folder file
- a folder of any of those, searched recursively — an Apple Mail export, a
  Thunderbird `Mail` folder, `~/Library/Mail`, a folder of `.eml` files.
  Files that are not mail are counted and left alone; a mail file that turns
  out to be broken is counted under "not understood" and the rest is read
- a single `.eml` or `.emlx`
- nothing at all: `path` if set; else the newest `takeout-*.zip` in
  `~/Downloads` that holds `Mail/*.mbox` (or the `Takeout` folder one unzips
  to); else, on a Mac, `~/Library/Mail`

## What gets imported

**One observation per message**, stream `email`:

| field | holds |
|---|---|
| `title` | the subject, decoded (`=?UTF-8?Q?…?=` becomes text) |
| `body` | what the writer wrote in this message — the text part, or the HTML part as text — with the quoted reply and the signature trimmed off (see below); at most 50,000 characters |
| `started_at` | the `Date` header in UTC; `utc_offset_sec` keeps the header's own offset (the sender's clock) |
| `direction` | `out` from one of your addresses, `self` from you to only you, `in` otherwise; `None` while nothing says which address is yours |
| `participants` | `From` as `sender`; `To`, `Cc` and `Bcc` as `recipient` — `email` handles |
| `value_json` | `labels` (Gmail) or `mailbox` (other stores), `thread`, `in_reply_to`, `attachments` `[{name, mime, size}]`, `cc_count`, `list_id`, `text_length`, `trimmed`, `invite`, `body_truncated` — only those that apply |
| `origin_app` | `gmail`, `apple_mail`, `thunderbird` or `email` |
| `last_modified_at` | when the export was made (see "Re-importing") |

- `thread` is Gmail's thread id (`X-GM-THRID`) in a Takeout, else the first
  Message-ID in `References` — the thread's first message — so every
  message of a thread carries the same value.
- **Attachments are never copied.** Each is described by its file name,
  type and size in bytes; the bytes are not read into the database. A
  forwarded message attached whole (`message/rfc822`) is described the same
  way, never read as this message's text. Apple Mail's `.partial.emlx`
  keeps attachments in a separate folder, so their size is unknown (`null`).
  A big message the Gmail source fetched without its attachments carries
  each one's size in an `X-Lifecrm-Omitted` line, and that size is used.
- A **calendar invitation** is kept as the email it is, marked `invite`.
  The event itself comes in through the [calendar](calendar.md) connector.

| kept | skipped (counted in the report) |
|---|---|
| everything in the inbox, the archive and your sent mail | Spam, Trash, Junk, Bin, Deleted Messages/Items, Drafts and Gmail's old Chat logs — the `skip_labels`, matched against Gmail labels and mailbox names |
| your own mail to a list | mailing-list and bulk mail (`List-Unsubscribe`, or `Precedence: bulk/list/junk`) — unless `include_lists = true` |
| calendar invitations | delivery reports: bounces, read receipts |
| | a second copy of a message already read (same Message-ID — Apple Mail keeps a note to self in INBOX and Sent) |
| | Apple Mail messages flagged deleted, draft or junk |
| | messages before `since` |

Gmail's read state (`Opened`, `Unread`) is left out of `labels`: it says
what was clicked, not what the message is, and would change on every
export.

## Bodies: what the person wrote

A reply carries everything said before it. Stored whole, every message of
a thread would hold the whole thread, and a search for Priya's advice would
find it in every reply that quoted it. So the body is trimmed to what *this*
message added; `text_length` keeps the length of the whole text, and
`trimmed: true` marks a body that was shortened.

- **The quoted reply** — "On Mon, Sep 22, 2026 at 5:40 PM Priya … wrote:"
  (also wrapped onto two lines, as Gmail does, and in German, French,
  Spanish, Dutch, Portuguese and Italian), followed by `>` lines — ends the
  message. Outlook's `-----Original Message-----` and its
  `From:`/`Sent:` header block do too.
- **An inline or bottom-posted reply** — writing between or after the
  quoted lines — keeps everything that is not quoted; only the attribution
  and the `>` lines go.
- **A forward** ("---------- Forwarded message ---------", "Begin forwarded
  message:", or an `Fwd:`/`FW:` subject over Outlook's header block) is kept
  whole: there, what was forwarded is the message.
- **The signature**: from a `-- ` line on, and a trailing "Sent from my
  iPhone" (and its Android, Outlook and other-language cousins).
- **HTML-only mail** (airline and booking confirmations) becomes text:
  scripts and styles dropped, entities decoded, a `<blockquote>` read as a
  quoted reply.
- A message that is *only* a quote — a reply with nothing added — keeps its
  subject and details, with no body.

## Dates and times

- `started_at` is the `Date` header; if there is none (or it does not
  parse), the newest `Received` header; failing that, the mbox's own
  separator line, then (Apple Mail) the `.emlx` plist's `date-received`. A
  message with no date anywhere is counted under "not understood".
- **The header's own offset is kept** in `utc_offset_sec`: Jake writing
  from Slovenia reads `+7200`. `-0000` means "zone unknown" and is stored
  as no offset.
- **Journal dates** follow the usual rule — your home `[time] timezone` and
  the rollover hour — like every other source, so an email sent at 01:30 is
  part of the previous day.

## Identity and re-importing

A message is identified by its **Message-ID** (angle brackets stripped), so
the same message from a Takeout, from Apple Mail and from a saved `.eml` is
one record. A message without one gets `sha1:` + a hash of its Date, From,
Subject and the start of its text — stable across re-exports.

`last_modified_at` is **when the export was made**: the time in a Takeout's
own name (`takeout-20261020T120000Z-001.zip`), else the file's modified
time. That makes re-importing do the right thing:

- **identical** — the same export again: skipped, and the dry run notes
  "already imported";
- **refreshed** — a newer export, the message unchanged: the time is
  updated silently;
- **update** — a newer export in which the message's labels changed
  (archived, starred, moved): applied, audit-logged;
- **conflict** — an *older* export that reads differently: never applied,
  surfaced in the report.

**Pick one source per account.** A Takeout and Apple Mail's copy of the same
Gmail account share Message-IDs, so they are the same rows — but one says
`labels`, the other `mailbox`, and each import of the newer one rewrites
them. Import Gmail by Takeout, everything else by Apple Mail (or
Thunderbird), and the two never meet.

## Deleted mail: the archive remembers

Texts **mirror** the phone (decided 2026-09-24). Mail does not mirror its
source: this connector declares no `present_ids`, and nothing is ever
removed because a later export lacks it.

- A Takeout holds all mail, but Gmail **empties Spam and Trash after 30
  days** on its own, and labels move — "missing from this export" would read
  as a deletion when it is housekeeping.
- Apple Mail's store holds what the account **syncs to this Mac**, which can
  be a window; an export is **one mailbox**.

To take an import back, `lifecrm imports retract <uuid>`.

## Attribution: handles, never names

Every address becomes an `email` handle, lower-cased — the same handle a
contact card or a calendar invitation produces, so linking it once
attributes every message, card and meeting at the same time. The name the
headers give ("Priya Natarajan <priya@acme-robotics.example>") is only the
handle's display name — the most frequent spelling across the archive.

```bash
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link email:priya@acme-robotics.example --entity <id>
.venv/Scripts/python -m lifecrm handles ignore email:booking@airfrance.example
```

Entries that are not addresses (`MAILER-DAEMON`, `undisclosed-recipients:;`)
are dropped from the participants and counted; their messages are kept.

## Volume

A decade of Gmail is several GB in one mbox. It is **streamed**: read line by
line from disk or straight out of the zip, one message held at a time, and a
message skipped for its label or as bulk mail is dropped after its headers,
before its body is parsed. What is kept per message is the trimmed text and
the details above — never the attachments' bytes. One row per message is
the right grain for mail (the point is the exact words), so nothing is
aggregated; for an archive larger than you want, `since` imports a window,
and a second import later with an earlier `since` adds the rest.

The whole scan is held in memory until it is written, as for every
connector: about 100 MB for 20,000 messages in testing.

## Known limitations

- **Signatures without a `-- ` line** — a name and title typed under the
  text — stay in the body; nothing tells them apart from the text.
- **Quote styles** beyond those listed come through as text. A reply whose
  attribution is in another language reads whole.
- **Outlook's `.msg` / `.pst`** are not read; save messages as `.eml`.
- **Encrypted mail** imports with its headers and nothing readable: an
  S/MIME message's body is an attachment (`smime.p7m`), a PGP one's is its
  armored ciphertext. Mail that is only *signed* reads normally.
- **Mailing lists** are recognised by their headers only. A list that sends
  without `List-Unsubscribe` or `Precedence` is kept; a person's mail sent
  through a list service that adds `List-Unsubscribe` is skipped unless
  `include_lists = true`.
- **Takeout's folder name** is `Mail` in English; the newest-Takeout search
  looks for that name only.

## First-real-import checklist (unproven until a real archive)

The readers were written from the formats as documented and commonly seen,
and the demo renders them, but none has been run against this owner's own
archive yet. On the first real dry run of each:

| check | how |
|---|---|
| [ ] Takeout: the message count matches Gmail's | `messages read` against the number Gmail shows for All Mail, Spam and Trash |
| [ ] Takeout: label names — `Sent`, `Spam`, `Trash`, `Opened`, `Archived`, `Category …` | `--json`, `facts.mailboxes` and a few records' `labels` |
| [ ] Takeout: one mbox per export, not split across zip parts | the `mbox` member is in the first zip; nothing "not understood" |
| [ ] Apple Mail export: `<Mailbox>.mbox/mbox` with a blank line between messages | `messages read` per mailbox matches Mail's count |
| [ ] Apple Mail store: the `.emlx` flags (deleted, draft, junk) | the `marked deleted` / `junk` counts look plausible |
| [ ] Full Disk Access denial reads as one line | run once without it |
| [ ] You are recognised | `you:` line, and `direction` has both `in` and `out` |
| [ ] Trimming on your correspondents' clients | look at a few `trimmed` replies with `sql "SELECT title, body FROM observations WHERE stream='email' LIMIT 20" --json` |
| [ ] Bulk detection | the bulk count against the Promotions tab; anything personal among them → `include_lists` |

Answers so far: none.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe email <zip|mbox|folder|eml>          # dry run
.venv/Scripts/python -m lifecrm observe email <...> --apply
.venv/Scripts/python -m lifecrm observe email                                 # path, else Downloads, else ~/Library/Mail
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm handles list --suggest
```

On a Mac the interpreter is `.venv/bin/python`.

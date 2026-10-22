# WhatsApp chats (Android and iPhone)

WhatsApp has no API for its history, and its own backups (Google Drive,
iCloud) are encrypted. What both phones *do* have is **Export chat**: one
chat at a time, as a plain text file. The `whatsapp` connector reads those
files into the observed tier. It never opens WhatsApp's databases, never
talks to the phone, and never sends anything.

Like texts and AI chats, these land in the **observed tier**: bulk records
that never pass the `/log` review gate, are never asserted as fact, and are
always cited. Nothing here can touch `entries`, `measurements` or `mentions`.
See [docs/observed-tier.md](observed-tier.md) for why.

## Exporting a chat

There is no "export everything": each chat is its own export. Export the
chats that matter — the family group, your partner, your best friend — and
re-export them whenever you want to catch up. Re-importing is always safe.

**Samsung / Android** — open the chat → **⋮** → *More* → **Export chat** →
*Include media* or *Without media*. Share it to the laptop: **Quick Share**
lands it in the Windows `Downloads` folder; OneDrive, Google Drive or email
work too. You get `WhatsApp Chat with <name>.txt`, or a `.zip` holding that
file and the media when media is included. The name is in the phone's
language — `WhatsApp Chat mit <name>.txt`, `Chat de WhatsApp con <name>.txt`,
`Conversa do WhatsApp com <name>.txt`, … — and reads the same.

**iPhone** — open the chat → tap the chat's name at the top → **Export
Chat** → *Attach Media* or *Without Media*. **AirDrop** it to the Mac (it
lands in `~/Downloads`), or *Save to Files* → iCloud Drive. You get
`WhatsApp Chat - <name>.zip` holding `_chat.txt` and any media.

WhatsApp caps how much one export holds (its help pages have said the most
recent 10,000 messages with media, 40,000 without), so for a long chat
*Without media* reaches further back. Check the oldest date in the dry run
against what the phone shows.

## Tell it which sender is you

The export names every sender only by how the phone displays them — and
that includes you. Put your name, exactly as your own lines show it, in
`config.toml`:

```toml
[connectors.whatsapp]
me = "Alex Rivera"                 # or a list: ["Alex Rivera", "Alex"]
# date_order = "mdy"               # only if the dry run says it had to guess
# timezone = "Europe/Ljubljana"    # only for an export made while away from home
# exports_dir = "~/Downloads"      # where 'observe whatsapp' looks when given no path
```

Without `me`, a one-to-one chat gives you away (the sender who is not the
chat's name), and so does the one sender common to every chat in a folder of
exports — the report says when it inferred you, and from what. A lone group
export names nobody as you: direction then stays unknown and the dry run
prints a **WARNING**. Set `me` before `--apply`: direction is recorded once,
and changing it later is a *conflict* (kept existing), not an update.

## Importing

Dry-run first — always. It is the review step.

```bash
.venv/Scripts/python -m lifecrm observe whatsapp "WhatsApp Chat - Rivera Family.zip"
```

```
whatsapp: WhatsApp Chat - Rivera Family.zip (1 chat export(s), 12 timestamped line(s))
  chat: Rivera Family — group, 7 message(s) 2026-10-10..2026-10-12, iphone format, dates month-first (e.g. 8/20/25) | WhatsApp Chat - Rivera Family.zip
  you: Alex Rivera ([connectors.whatsapp] me)
  times read as America/Chicago — the export records no zone
  whatsapp: 7 new, 0 changed | 2026-10-10..2026-10-12 | e.g. 2026-10-12 them: Thank you for coming, mijo. Drive safe.
  handles: 2 (1 by the name the export shows, 1 by phone number) — review with 'lifecrm handles list --suggest'
  media: 1 file(s) in the export (path kept, never the bytes), 0 named but not in the export, 0 exported without media
  skipped: 1 × deleted message ('This message was deleted')
  skipped: 1 × system line: encryption notice
  skipped: 3 × system line: group change
dry run — nothing written. Re-run with --apply to write.
```

Read the chat line: **one-to-one or group**, the **date span**, and which
way the **dates** were read. A wrong year, a span that does not match the
chat, or a `WARNING` means stop and fix the setting it names. Then add
`--apply`; the import uuid it prints undoes the whole run with
`lifecrm imports retract <uuid>`. `--json` carries the same facts for
scripts.

Accepted targets:

- the `.zip` either phone made, the `.txt` (Android), or `_chat.txt`
- the folder an unzipped export leaves (`WhatsApp Chat - <name>/`)
- a folder holding several exports — each is read as its own chat, and
  files that are not WhatsApp exports are ignored, so pointing it at
  `Downloads` is fine
- nothing at all: `exports_dir` if set, else `~/Downloads` when it holds an
  export

A browser's `(1)` on a second download of the same name is understood: it
is the same chat, and its messages count as duplicates, not new ones.

**The file name names the chat.** It is the shape that is recognised, not
the English words: a few words, `WhatsApp`, then "with" in the phone's
language (`with`, `mit`, `con`, `com`, `avec`, `met`, `med`, `dengan`, `z`,
`с`, …) or the iPhone's ` - `, then the chat's name. So `WhatsApp Chat mit
Max.txt` is the one-to-one chat with Max, with the same message ids as
`WhatsApp Chat with Max.txt`. A file whose name mentions WhatsApp in some
other wording is recognised by its first line instead, and its whole name is
taken as the chat's name — the dry run then prints a **WARNING**: the name
is part of every message's id and decides one-to-one vs group, so rename the
file `WhatsApp Chat with <chat name>.txt` before `--apply` if it is wrong.

## What gets imported

**One observation per message**, stream `whatsapp`, with the chat's name as
the title on every row — each row is self-describing, and search finds a
chat by its name.

| kept | not kept (counted in the report) |
|---|---|
| every message, multi-line ones whole | system lines: the encryption notice, created/added/removed/left/joined, subject and icon changes, security-code and number changes, call notices |
| media messages, with the caption as the body | "This message was deleted" — nothing is left of it |
| `media_path` when the file is in the export | "Waiting for this message" — the phone never got it |
| media sent "without media", marked `media_omitted` | view-once media (`null`) |
| `edited: true` for "<This message was edited>" | |

- **value_json** carries `group`, `edited`, `media` (image, video, audio,
  voice, sticker, gif, document, contact), `attachment` (the file's name),
  and `media_omitted` — only the fields that apply.
- **Media is never copied.** `media_path` is the file's path *relative to
  the export* (the zip member or the file beside the `.txt`); the bytes stay
  where they are.
- A system line of a kind this connector has no pattern for is counted under
  "not understood", never imported as somebody's message.

## Dates and times

Every line starts with a timestamp in the phone's locale, and nothing in the
file says which locale:

```
[27/07/2026, 21:30:05] Jake Morales: see you at 7      iPhone, day-first
[7/27/26, 9:30:05 PM] Jake Morales: see you at 7       iPhone, US
27/07/2026, 21:30 - Jake Morales: see you at 7         Android, day-first
7/27/26, 9:30 PM - Jake Morales: see you at 7          Android, US
```

- **Day or month first** is decided per file from *all* its dates: a first
  number over 12 can only be a day; a second one over 12 means month-first.
  A file whose every date fits both ways (a short chat early in a month) is
  read by `date_order`, else day-first — and the dry run warns that it
  guessed. Dates that fit neither order fail with both examples named.
- **No zone.** WhatsApp writes times in the phone's zone *at the moment of
  export*. They are read in your `[time] timezone`; an export made while
  travelling can say where with `[connectors.whatsapp] timezone`. Journal
  dates follow the usual rule — home zone and the rollover hour — like every
  other source.
- **Android has no seconds.** Those messages land on the minute; the iPhone
  writes seconds.

## Identity and re-importing

The export carries no message ids, so one is derived from the chat's name,
the instant, the sender, the text, and the position among messages that are
identical in all four (two "ok"s in the same Android minute stay two). A
later export of the same chat therefore matches what is already stored:

- **identical** — skipped silently. Re-importing the same file is a no-op,
  and the dry run notes "already imported".
- **conflict** — the same message reads differently but is not newer (it
  cannot be: the export has no edit times). **Never applied**, surfaced in
  the report.

Two consequences, by design:

- **Renaming the chat** (or the contact a one-to-one chat is named after)
  before the next export makes its messages look new. Keep the name, or
  retract the old import first.
- **An edit made after an import** arrives as a new row beside the old
  wording; the old one stays. The edited row carries `edited: true`.

## Deleted messages: the archive remembers

Texts and AI chats **mirror** their source (decided 2026-09-24): each of
those scans is the account's complete current state, so what it no longer
holds was deleted. A WhatsApp export is not that:

- it is **one chat**, never the account;
- it is **capped** — an older export can hold messages a newer one has
  dropped without anyone deleting anything;
- a message **deleted for everyone** stays in place as "This message was
  deleted", and one deleted "for me" simply vanishes — indistinguishable from
  truncation.

Nothing in an export tells a deletion from truncation, so this connector
removes nothing: a message deleted in WhatsApp stays archived. To take an
import back, `lifecrm imports retract <uuid>`.

## Attribution: handles, never names

The export identifies a sender only as the phone displays them:

- **someone in your contacts** → their contact name → handle
  `app_account:whatsapp:<name as shown>`, with that name as its display name
  — the export's only identifier for them;
- **someone not in your contacts** → their number → a `phone` handle, the
  same one their texts and calls produce. The export gives no name for it.
- **a group member not in your contacts who chose a name** → WhatsApp shows
  `~ Sam` → handle `app_account:whatsapp:~Sam`, display name `Sam`. The `~`
  stays in the handle: it is the export's only sign that this is *not* your
  contact Sam, so the two never share a handle — and a stranger who calls
  themself by your own name is never taken for you. Save them as a contact
  later and the next export shows them as `Sam`: a new handle (link both),
  and their messages import again under it, as for any renamed contact.

Linking is the usual one decision per handle, past and future:

```bash
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link "app_account:whatsapp:Linda Rivera" --entity <id>
```

`--suggest` offers entities whose name or alias matches the display name;
it never links anything itself.

## Known limitations

- **One chat per export**, capped by WhatsApp; there is no bulk export.
- **Names are the identity.** Two different people saved under the same
  name on your phone are one handle; one person saved under two names is two.
- **Calls** appear only as notices and are counted, not imported.
- **Polls, events and other newer message types** come through as their
  text, when the export writes any.
- **English markers.** Timestamps and senders are read whatever the phone's
  language, but the markers — "This message was deleted", `<Media omitted>`,
  `<This message was edited>`, system lines — are recognised in English
  only. In another language they come through as message text or as "not
  understood". (Export *file names* in other languages are understood —
  see "The file name names the chat" above.)

## Commands

```bash
.venv/Scripts/python -m lifecrm observe whatsapp <export>          # dry run
.venv/Scripts/python -m lifecrm observe whatsapp <export> --apply
.venv/Scripts/python -m lifecrm observe whatsapp                   # exports_dir or ~/Downloads
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm handles list --suggest
```

On a Mac the interpreter is `.venv/bin/python`.

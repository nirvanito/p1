# Instagram and Messenger messages (Meta export)

Meta has no API for message history. What it does have is **Download your
information**: the whole account, made on Meta's servers and handed over as
a zip. The `meta_messages` connector reads the messages in that zip —
Instagram DMs and Facebook Messenger — into the observed tier. It never logs
in, never calls Meta, and never reads a photo's bytes.

Like texts and WhatsApp, these land in the **observed tier**: bulk records
that never pass the `/log` review gate, are never asserted as fact, and are
always cited. Nothing here can touch `entries`, `measurements` or `mentions`.
See [docs/observed-tier.md](observed-tier.md) for why.

## Getting the export

Both apps go through **Accounts Center**, the same on Android, the iPhone and
the web, and the export is the same whichever phone asks for it.

**Instagram** — profile → **☰** → *Accounts Center* → *Your information and
permissions* → **Download your information** → *Download or transfer
information* → pick the Instagram account → *Some of your information* →
tick **Messages** → *Download to device* → *Date range: All time* →
**Format: JSON** → *Create files*.

**Facebook / Messenger** — **☰ Menu** → *Settings & privacy* → *Settings* →
*Accounts Center* → *Your information and permissions* → **Download your
information** → the same steps, picking the Facebook account and
**Messages**. (On a computer: accountscenter.facebook.com or
accountscenter.instagram.com → *Your information and permissions*.)

Two settings matter:

- **Format: JSON.** The default is HTML, which is a web page, not data, and
  is refused in one line: request the export again in JSON.
- **Date range: All time.** A shorter range is fine to import, but only the
  range comes in — and nothing in the files says which range it was (see
  "Deleted messages" below).

Meta emails when the files are ready (minutes to days), and they stay
downloadable from the same screen for a few days. You get
`instagram-<username>-<date>-<id>.zip` or `facebook-<name>-<date>-<id>.zip`;
a big account comes in **several parts** — download every part into one
folder and give the importer the folder.

*Not verified against the live apps:* the exact menu labels above (Meta
renames them often — the path through Accounts Center has stayed the same),
and how a split export names its parts.

## Importing

Dry-run first — always. It is the review step.

```bash
.venv/Scripts/python -m lifecrm observe meta_messages instagram-alexrivera.atx-2026-10-22-dEm0iG7x.zip
```

```
meta_messages: instagram-alexrivera.atx-2026-10-22-dEm0iG7x.zip (Instagram: 3 conversation(s), 3 message file(s))
  you on Instagram: Alex Rivera (the export's own profile)
  conversation: Maya Chen — one-to-one, 8 message(s) 2026-09-26..2026-10-14
  conversation: Bled crew — group, 5 message(s) 2026-10-06..2026-10-20
  message requests: 1 conversation(s), 1 message(s) left out — set [connectors.meta_messages] include_requests = true to import them
  text: 12 string(s) repaired from Meta's Latin-1 escaping of UTF-8
  instagram_dm: 13 new, 0 changed | 2026-09-26..2026-10-20 | e.g. 2026-10-20 them: Lakeside place is booked for the four of us
  handles: 3 by the name the export shows (Meta exports carry no account ids) — review with 'lifecrm handles list --suggest'
  media: 1 message(s) with the file in the export (path kept, never the bytes), 0 named but not in the export
  skipped: 1 × message in a message request (include_requests = true imports them)
  skipped: 1 × system line: group change
  skipped: 2 × system line: reaction notice
  skipped: 1 × system line: unsent message
dry run — nothing written. Re-run with --apply to write.
```

Read the **you** line (who your own messages are), the conversations (the ten
largest are listed) and the **date span**. A `WARNING` means stop and fix
the setting it names. Then add `--apply`; the import uuid it prints undoes
the whole run with `lifecrm imports retract <uuid>`. `--json` carries the
same facts for scripts.

Accepted targets:

- the export `.zip`, or the folder it unzips to
- a folder holding exports — the parts of a split export, or an Instagram
  and a Facebook export side by side; anything else in it is ignored, so
  `Downloads` is fine
- nothing at all: `exports_dir` if set, else `~/Downloads` when it holds a
  zip named the way Meta names them

Both the current layout (`your_instagram_activity/messages/...`,
`your_facebook_activity/messages/...`, `your_activity_across_facebook/...`)
and the older one (`messages/inbox/...` at the top) are read. An older export
says which app it is from through its profile file or its zip's name; if
neither does, the dry run says so and `platform` settles it.

```toml
[connectors.meta_messages]
# me = "Alex Rivera"            # only if the dry run cannot tell which participant is you
# include_requests = true       # import message requests too (default: counted, left out)
# platform = "instagram"        # only for an old export that does not say ("messenger" for Facebook)
# exports_dir = "~/Downloads"   # where 'observe meta_messages' looks when given no path
```

## What gets imported

**One observation per message**: stream `instagram_dm` or `messenger`, with
the conversation's name as the title on every row (the other person's name
for a one-to-one chat, the group's name otherwise).

| kept | not kept (counted in the report) |
|---|---|
| every message a person sent, as its text | "Liked a message", "reacted ❤ to your message" |
| shared posts and reels (`share`), with or without words | "unsent a message" — nothing is left of it |
| photos, videos, voice messages, files, GIFs, stickers — as paths | calls: "called you", "started a video chat", Messenger's call lines |
| archived conversations, and Messenger's `e2ee_cutover/` history | group changes: named, photo, added/removed/left, nicknames, themes, pins |
| message requests, only with `include_requests = true` | "You are now connected on Messenger" and the like |

- **body** is the person's own words. Meta's stand-in "X sent an
  attachment." is taken off: a share or photo with no words has no body.
- **value_json** carries only what applies: `group` (more than two
  participants), `folder` (`archived_threads`, `e2ee_cutover`,
  `message_requests`, `filtered_threads` — absent for the inbox), `share`
  `{link, text, owner}`, `media` `[{kind, uri}]`, `reactions` `[{by,
  reaction}]` where `by` is a handle value, and `attachment_missing` when
  Meta says something was sent but exported nothing of it.
- **Media is never copied.** `uri` is the file's path inside the export and
  `media_path` repeats the first one when the file is really there; the
  bytes stay in the zip.
- A message with no text and only fields this reader does not know is
  counted under "not understood", never imported as an empty row.

## The text quirk: Meta's escaping

Meta writes every string as its UTF-8 *bytes*, each escaped as if it were a
Latin-1 character: "é" is `Ã©` in the file, which reads as "Ã©".
Every string — text, names, titles, reactions — is repaired with
`s.encode("latin-1").decode("utf-8")` when that round-trips. Text that is
already right cannot round-trip (an emoji is past U+00FF; a lone "é" is not
valid UTF-8 on its own), so it passes through unchanged — and an export Meta
someday writes correctly reads the same. The report counts the repairs.

## Dates and times

Each message carries `timestamp_ms`, a UTC instant, so times are exact (to
the second; milliseconds are dropped). Journal dates follow the usual rule —
home timezone and the rollover hour — like every other source: a message at
3:30 at night belongs to the day before.

## Identity and re-importing

The export has no message ids, so one is derived: `meta:` + SHA-1 of the
app, the conversation's **folder name** (`mayachen_17841400000000187`), the
instant, the sender as shown, the text with what was attached, and the
position among messages identical in all of those (two "ok"s in one
millisecond stay two). The folder name, not the folder it sits in: a chat
archived since the last export keeps its ids.

- **identical** — skipped. Re-importing the same file is a no-op, and the
  dry run notes "already imported".
- **a reaction added since** — updates the row on Instagram, which dates
  reactions. Messenger does not, so there it is a **conflict**: never
  applied, kept existing, listed in the report.
- **an edit** — the export keeps no edit history: an edited message arrives
  as a new row beside the old wording.
- **renamed** — a person who renames their profile, or a group renamed,
  changes what the export shows; see the next sections.

## Deleted messages: the archive remembers

Texts mirror the phone (decided 2026-09-24): each backup is the account's
complete current state. A Meta export need not be — the request form offers
a **date range**, and nothing in the files says which range was asked for,
so a later export can lack years of messages nobody deleted. A message
unsent in the app is gone from the next export without a trace. Nothing
tells a deletion from a shorter range, so this connector removes nothing: a
message deleted on Instagram or Messenger stays archived. To take an import
back, `lifecrm imports retract <uuid>`.

## Attribution: handles, never names

The export names people **only as the app displayed them** — no account id,
no username, no number. So a sender becomes:

- `app_account:instagram:<name as shown>` or
  `app_account:messenger:<name as shown>`, with that name as its display
  name — the export's only identifier for that person;
- a deleted or deactivated account, which Meta shows as "Instagram User" or
  "Facebook user" for everyone, is scoped to its conversation —
  `instagram:Instagram User (<folder>)` — so deleted strangers never merge
  into one person.

Consequences: someone who renames their profile is a second handle (link
both); two people shown with the same name are one handle. Linking is the
usual one decision per handle, past and future:

```bash
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link "app_account:instagram:Maya Chen" --entity <id>
```

**Which participant is you**, in order: `[connectors.meta_messages] me`; the
export's own profile (`personal_information.json` on Instagram,
`profile_information.json` on Facebook); the one participant common to
every conversation. Without any of those, direction stays unknown and the
dry run prints a **WARNING** — set `me` before `--apply`: direction is
recorded once, and changing it later is a conflict, not an update.

## Known limitations

- **Names are the identity** (above). The export has nothing better.
- **Messenger's end-to-end encrypted chats** after the cutover are not in
  this export at all. Messenger offers them separately (secure storage →
  download), in a different format this connector does not read;
  `e2ee_cutover/` holds only the history from before encryption.
- **English notices.** "Liked a message", "unsent a message", "named the
  group" and the rest are recognised in English. An account set to another
  language may write them otherwise; they then come through as message text.
- **The profile file's labels** ("Name", "Username") are read in English
  too; if they differ, the one participant common to every conversation, or
  `me`, still says which one is you.
- **Memory.** Each `message_N.json` is read whole; Meta caps them at about
  10,000 messages each, so even a large account is read a file at a time.
  The media is never read.

## First real import — check these

The reader was written from Meta's documented and commonly seen JSON export
and the demo renders it, but it has not yet been run against a real export
from this owner's accounts. On the first real one, check in the dry run:

1. the **you** line names you, from the export's own profile;
2. accented names and emoji read correctly ("Tía", "❤️") — the repair count
   is non-zero;
3. the conversation list matches the app's, with the right one-to-one /
   group split, and spans that go back as far as the export should;
4. the skipped lines hold notices, not real messages — and no "not
   understood" lines (if there are, say what they show);
5. a shared reel shows as `share` with its link, and a photo has a
   `media_path` (Format: JSON with media included).

Anything off — say so; the notice patterns are a one-line change each.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe meta_messages <zip|folder>          # dry run
.venv/Scripts/python -m lifecrm observe meta_messages <zip|folder> --apply
.venv/Scripts/python -m lifecrm observe meta_messages                       # exports_dir or ~/Downloads
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm handles list --suggest
```

On a Mac the interpreter is `.venv/bin/python`.

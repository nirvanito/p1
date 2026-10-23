# Telegram chats (Telegram Desktop export)

Telegram keeps your history in its cloud and gives it back through
**Telegram Desktop**, as a folder with a `result.json` beside the media. The
`telegram` connector reads that JSON into the observed tier. It never logs
in, never talks to Telegram, and never reads a photo's bytes.

Like texts and WhatsApp, these land in the **observed tier**: bulk records
that never pass the `/log` review gate, are never asserted as fact, and are
always cited. Nothing here can touch `entries`, `measurements` or `mentions`.
See [docs/observed-tier.md](observed-tier.md) for why.

## Getting the export

Only **Telegram Desktop** exports — the Windows, macOS and Linux app from
desktop.telegram.org (on a Mac, the "Telegram Desktop" app, not the App
Store's "Telegram"). The phone apps cannot. The export is the same on the
Windows laptop and the MacBook.

**One chat** — open the chat → **⋮** (top right) → **Export chat history**
→ choose what to include (photos, files, size limit) → **Format:
Machine-readable JSON** → *Export*. You get
`Downloads/Telegram Desktop/ChatExport_<date>/result.json`.

**The whole account** — *Settings* → *Advanced* → **Export Telegram data**
→ tick *Account information* (this is what says which sender is you),
*Personal chats*, *Private groups*, and whatever else you want →
**Format: Machine-readable JSON** → *Export*. You get
`Downloads/Telegram Desktop/DataExport_<date>/result.json`. Telegram may
first ask you to confirm the export from a phone that is already logged in,
and a newly logged-in desktop can be made to wait before exporting.

Two settings matter:

- **Machine-readable JSON.** The default is HTML, which is a web page, not
  data, and is refused in one line: export again as JSON.
- **The date range and media limits** you pick are what comes in — and
  nothing in `result.json` says what you picked (see "Deleted messages").

*Not verified against the live app:* the exact menu labels, and the
confirmation and waiting rules, which Telegram changes from time to time.

## Importing

Dry-run first — always. It is the review step.

```bash
.venv/Scripts/python -m lifecrm observe telegram "Downloads/Telegram Desktop/DataExport_2026-10-22"
```

```
telegram: DataExport_2026-10-22 (full export, 4 chat(s))
  you: Alex Rivera (telegram:user700000100, the export's personal_information)
  chat: Sam Okafor — one-to-one, 7 message(s) 2026-10-21..2026-10-22
  chat: Saturday Climbers — group, 3 message(s) 2026-10-22..2026-10-22
  chat: Saved Messages — saved messages, 1 message(s) 2026-10-06..2026-10-06
  telegram: 11 new, 0 changed | 2026-10-06..2026-10-22 | e.g. 2026-10-22 you: Bringing snacks 🍌
  handles: 2 Telegram account(s), by user id — review with 'lifecrm handles list --suggest'
  media: 1 file(s) in the export (path kept, never the bytes), 0 named but not in the export, 1 left out by the export settings
  not chats (not imported): contacts (2)
  skipped: 1 × message in a channel or public group (include_channels = true imports them)
  skipped: 1 × service: create_group
  skipped: 1 × service: phone_call
  skipped: 1 × service: pin_message
dry run — nothing written. Re-run with --apply to write.
```

Read the **you** line, the chats (the ten largest are listed) and the
**date span**. A `WARNING` means stop and fix the setting it names. Then
add `--apply`; the import uuid it prints undoes the whole run with
`lifecrm imports retract <uuid>`. `--json` carries the same facts.

Accepted targets:

- `result.json` itself, the export folder holding it, or a `.zip` of that
  folder
- a folder holding several exports — `Downloads/Telegram Desktop` keeps
  every `ChatExport_`/`DataExport_` it made — each read in turn; an old
  HTML export among them is left out with a warning
- nothing at all: `exports_dir` if set, else `~/Downloads/Telegram Desktop`
  when it holds an export

```toml
[connectors.telegram]
# me = "user123456789"          # only for single-chat exports that cannot tell (your id, or your name as shown)
# include_channels = true       # import channels and public groups too (default: counted, left out)
# exports_dir = "~/Downloads/Telegram Desktop"
```

## What gets imported

**One observation per message**, stream `telegram`, with the chat's name
as the title on every row.

| kept | not kept (counted in the report) |
|---|---|
| one-to-one chats, private groups and supergroups | service messages, by action: `phone_call`, `create_group`, `invite_members`, `pin_message`, `edit_group_title`, ... |
| Saved Messages, as direction `self` | channels and public supergroups (a broadcast, not a conversation) — unless `include_channels = true` |
| chats you have left (`left_chats`) | bot chats (a program, not a person) — always |
| photos, files, voice and video messages, stickers — as paths | the full export's other sections: contacts, sessions, ... (listed) |
| polls (the question as body), locations, venues | |

- **body** is the text as the chat shows it. Telegram writes formatted text
  as a list of runs (`["Opening week is ", {"type": "bold", "text": "half
  price"}]`); they are joined back into one string.
- **value_json** carries only what applies: `group`, `chat_type` (when not
  a one-to-one chat), `reply_to` (the id of the message answered),
  `edited`, `forwarded`, `media` `{kind, file, mime_type, duration_seconds,
  sticker_emoji, omitted}`, `location` `{lat, lon}`, `venue`, `poll`
  `{question, answers}`, `contact_shared`, and `reactions` `[{reaction,
  count, by}]` where `by` lists handle values.
- **Media is never copied.** `file` is the path inside the export, and
  `media_path` repeats it when the file is really there. A file the export
  settings left out ("File not included") is marked `omitted`.
- **Forwarded messages** say so (`forwarded: true`); who they came from is
  a name, and names never go into a record.

## Dates and times

Each message carries `date_unixtime`, a UTC instant, so times are exact.
Journal dates follow the usual rule — home timezone and the rollover hour —
like every other source. An export old enough to lack `date_unixtime` is
read from `date` (the exporting computer's clock) in your home timezone.

## Identity and re-importing

Telegram has real ids: `tg:<chat id>:<message id>`, stable across exports,
so a later export of the same chat matches what is already stored — the
whole-account export and a single-chat export of the same chat give the
same ids.

- **identical** — skipped. Re-importing the same folder is a no-op, and the
  dry run notes "already imported".
- **an edit** — carries its own time (`edited_unixtime`), which becomes the
  row's `last_modified_at`: a message edited since the last import
  **updates** on the next one, newer-wins.
- **a reaction added since** — its date counts too, so it updates as well.

## Deleted messages: the archive remembers

Texts mirror the phone (decided 2026-09-24): each backup is the account's
complete current state. A Telegram export is what you asked for — one chat
or a chosen set, a date range, media limits — and nothing in `result.json`
says what was chosen, so a later export can lack messages nobody deleted.
This connector removes nothing: a message deleted in Telegram stays
archived. To take an import back, `lifecrm imports retract <uuid>`.

## Attribution: Telegram's user ids

Every sender has an id in the export (`from_id: "user123456789"`). It
becomes `app_account:telegram:user123456789`, with the name shown as its
display name. Unlike a name, the id survives a rename and a deleted
account (whose name the export leaves empty). The other side of a
one-to-one chat is the chat's own id; a channel posts as
`telegram:channel<id>`.

```bash
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link app_account:telegram:user700000166 --entity <id>
```

**Which sender is you**, in order: the whole-account export's
`personal_information` (your user id); `[connectors.telegram] me`; the
sender in a one-to-one chat who is not the chat, or the sender of Saved
Messages. A single group's export has none of those: direction then stays
unknown and the dry run prints a **WARNING** — set `me` before `--apply`.

## Known limitations

- **Read whole.** `result.json` is one JSON document, read at once. A
  whole-account export of a busy account can run to hundreds of MB; export
  chats separately, or a date range, if memory runs short.
- **Calls** are service messages, counted and not imported.
- **Group members who never wrote** are not in the export's messages, so a
  group message's recipients are the people who wrote in that chat, plus
  you.
- **Phone numbers** are not linked: messages carry user ids only, and the
  contact list (which has numbers) carries no ids. Link the Telegram handle
  to the person once, and it meets their texts and calls through the entity.
- **Secret chats** are not exported by Telegram at all.

## First real import — check these

The reader was written from Telegram Desktop's JSON export as documented and
commonly seen, and the demo renders it, but it has not yet been run against
a real export from this owner's account. On the first real one, check in the
dry run:

1. the **you** line names you, from `personal_information`;
2. a one-to-one chat's messages split into you / them correctly;
3. the chat list and spans match what you exported;
4. the skipped lines hold service messages and channels only — and no "not
   understood" lines (if there are, say what they show);
5. a photo has a `media_path` when photos were included.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe telegram <folder|result.json|zip>   # dry run
.venv/Scripts/python -m lifecrm observe telegram <...> --apply
.venv/Scripts/python -m lifecrm observe telegram                            # exports_dir or Telegram Desktop's folder
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm handles list --suggest
```

On a Mac the interpreter is `.venv/bin/python`.

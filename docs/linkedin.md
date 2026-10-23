# LinkedIn (connections and messages)

Who you know professionally, where they work now, and what they wrote to
you there is the part of a life the address book does not hold. LinkedIn
has no API an individual can use for it, but it will hand the whole thing
over as a data export. The `linkedin` connector reads that export. It never
logs in, never scrapes, and never sends anything.

Connections and messages land in the **observed tier**: bulk records that
never pass the `/log` review gate, are never asserted as fact, and are
always cited. Nothing here can touch `entries`, `measurements` or
`mentions`, and nothing here creates a relationship — "Priya works at Acme
Robotics" becomes a confirmed `entity_links` row only when you state it in
a reviewed draft or with `entities link add`. See
[docs/observed-tier.md](observed-tier.md) for why.

## Getting the export

The same on both combos — LinkedIn is a website, and the request works from
the app too:

- **Laptop (Windows or Mac), linkedin.com** — your photo (**Me**) →
  *Settings & Privacy* → *Data privacy* → **Get a copy of your data**.
- **Phone app (Samsung or iPhone)** — your photo → *Settings* → *Data
  privacy* → **Get a copy of your data**. Download the result on the laptop:
  the zip is easier to keep there.

Pick *Want something in particular?* and tick **Connections**, **Messages**
and **Invitations** (and *Profile*, which helps tell which profile is yours),
or take the whole archive. LinkedIn emails a link when it is ready —
minutes for a few files; the full archive can take a day, and arrives as a
second zip (`Basic_LinkedInDataExport_<MM-DD-YYYY>.zip` first, then
`Complete_LinkedInDataExport_…`). Either one imports; the newer one is the
fuller picture.

*Unverified without a real export:* the exact menu wording (LinkedIn moves
these pages around), the zip names, and whether every account's
messages.csv has the same columns — newer exports have been seen adding
`ATTACHMENTS` and `IS MESSAGE DRAFT`, which are read when present.

## Importing

Dry-run first — always:

```bash
.venv/Scripts/python -m lifecrm observe linkedin ~/Downloads/Basic_LinkedInDataExport_10-21-2026.zip
```

```
linkedin: Basic_LinkedInDataExport_10-21-2026.zip (3 connection(s), 2 message(s) in 1 conversation(s); read: Connections.csv, Invitations.csv, Profile.csv, messages.csv)
  you: linkedin:in/alex-rivera-pm (Alex Rivera) — from Invitations.csv (the profile on your side of every invitation)
  conversation: Priya Natarajan — one-to-one, 2 message(s) 2026-10-02..2026-10-02
  linkedin_connection: 3 new, 0 changed | 2017-06-13..2024-07-15 | e.g. 2024-07-15 Sam Okafor — Physical Therapist at Capital City Physio
  linkedin_message: 2 new, 0 changed | 2026-10-02..2026-10-02 | e.g. 2026-10-02 you: Thank you, Priya. Your advice to open with the warehouse st…
  handles: 5 (3 LinkedIn profile(s), 2 email address(es)) — review with 'lifecrm handles list --suggest'
  skipped: 2 × line of LinkedIn's note above the Connections.csv header
  not imported: 2 other file(s) in the export (e.g. Positions.csv, Skills.csv)
dry run — nothing written. Re-run with --apply to write.
```

Read the **you:** line first: it decides which messages are yours and what
mirroring may remove. Then write:

```bash
.venv/Scripts/python -m lifecrm observe linkedin ~/Downloads/Basic_LinkedInDataExport_10-21-2026.zip --apply
```

Accepted targets: the export `.zip`, the folder it unzips to, or one of its
CSVs on its own (`Connections.csv` or `messages.csv`, recognised by its
header if renamed). With no path, the connector uses `[connectors.linkedin]
path`, else the newest `*LinkedInDataExport*` zip or folder in
`~/Downloads`.

### Settings

```toml
[connectors.linkedin]
profile_url = "https://www.linkedin.com/in/alex-rivera-pm"   # your own profile
path = "~/Downloads/Basic_LinkedInDataExport_10-21-2026.zip" # optional default
```

`profile_url` is only needed when the dry run says it could not tell which
profile is yours. Otherwise the export says it: the inviter of every
OUTGOING invitation and the invitee of every INCOMING one
(`Invitations.csv`); else the message sender whose name is `Profile.csv`'s
(or `[owner] name`); else the one profile in every conversation.

## What gets imported

Of the export's dozens of files, four are read. `Connections.csv` and
`messages.csv` are imported; `Invitations.csv` and `Profile.csv` only tell
which profile is yours. Everything else — Positions, Skills, Endorsements,
Reactions, ad targeting — is counted by name and left alone.

**`linkedin_connection`** — one per connection:

| field | holds |
|---|---|
| `title` | the connection's name as LinkedIn shows it |
| `body` | "Position at Company" — what search finds ("VP Product at Acme Robotics") |
| `started_at` / `journal_date` | the *Connected On* date, at local noon (a date, not a time — finance's rule) |
| `value_json` | `company`, `position`, `url`, `email`, as the file holds them |
| participants | `app_account linkedin:<profile path>` and, when they share it, their `email` |
| `last_modified_at` | when the export was made (the zip's own member time) |

**`linkedin_message`** — one per message:

| field | holds |
|---|---|
| `title` | the conversation's title, else its subject, else the other people's names |
| `body` | the content as plain text — LinkedIn stores rich messages as HTML (`<p>`, `<br>`, links, `&amp;`), which is taken off |
| `direction` | `out` when the sender is your profile, else `in` |
| `value_json` | `conversation_id`, `folder`, `subject` (when not the title), `group`, `attachments` (a count) |
| participants | the sender (`sender`) and every recipient (`recipient`), by profile |

Skipped and counted: drafts, the SPAM folder, and messages with no text and
no attachment. A message whose sender has no profile URL ("LinkedIn
Member", a deleted account) is kept, with its sender unattributed and the
name LinkedIn showed kept as `sender_shown_as`.

Times: messages are UTC as LinkedIn writes them (`2026-10-02 13:40:00
UTC`); journal dates use your home timezone and the rollover hour, like
every other source.

## Handles, never names

A person is their **profile path**: `https://www.linkedin.com/in/Priya-N/`
becomes `app_account linkedin:in/priya-n` (lower-cased and URL-decoded, so
every file spells it the same way). The name is only the handle's display
name — what `lifecrm handles list --suggest` matches against your entities.
When a connection shares an email address, that is a second handle on the
same row, and it is the one that meets texts, calendar invites and contact
cards: link `email:priya@acme-robotics.example` once and the connection,
the calendar invites and the card all resolve to Priya.

## Re-importing, and what wins

- A connection is keyed on its profile path (`linkedin:in/<slug>`); a row
  with no URL on its name and Connected On date. A later export with a new
  job **updates** the row — its export time is newer — instead of standing
  as a conflict; unchanged rows are only re-dated (`re-dated by this newer
  export` in the report).
- A message has no id in the export, so one is derived from the
  conversation, the instant, the sender, the raw content and an ordinal
  among identical messages — two "ok"s in one minute stay two.

## Deleted connections leave; messages are remembered

`Connections.csv` is the complete current list, so a connection you
removed — or who removed you — is **removed** by the next import. Removal
is the shared write layer's: scoped to your own profile as the account (an
export that cannot say whose it is removes nothing, and says so), a
database snapshot first, the mass-removal guard, and a connection that an
entry or a relationship cites is hidden rather than deleted.

Messages are **remembered**. The export holds what the inbox holds, but a
thread archived away, a conversation deleted by the other side or a Basic
export cut short cannot be told from a deletion — and a message is evidence
of something said. Nothing removes them; `lifecrm imports retract <uuid>`
undoes a whole run.

## Known limitations

- **Emails are rare.** LinkedIn includes one only for connections who allow
  it (its own note at the top of Connections.csv says so).
- **Two spellings of one person.** Some exports write a profile as its
  member id (`/in/ACoAAB…`) in messages.csv and as the vanity URL in
  Connections.csv. They are two handles: link both to the same entity.
- **A changed vanity URL** is a new handle — and, for a connection, a
  removal plus an addition.
- **Connected On** is read in English (`15 Mar 2021`); a localised export's
  month names are reported as unreadable, never guessed.
- **Invitations, endorsements, posts** are not imported.

## First real import — check these

Written from LinkedIn's documented and commonly seen export, and rendered
by the demo, but not yet run against this owner's real export. On the first
one, check in the dry run:

1. the **you:** line names your own profile (else set `profile_url`);
2. the connection count matches *My Network → Connections*;
3. a known connection's position and company read right;
4. a message you sent reads `you:` and one you received `them:`;
5. message bodies read as plain text, with no `<p>` or `&amp;` left;
6. `not understood:` is empty, or says something worth fixing.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe linkedin <zip|folder|csv>          # dry run
.venv/Scripts/python -m lifecrm observe linkedin <zip|folder|csv> --apply
.venv/Scripts/python -m lifecrm observe linkedin                           # newest export in Downloads
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link app_account:linkedin:in/<slug> --entity <id>
.venv/Scripts/python -m lifecrm imports retract <uuid>
```

On the Mac the interpreter is `.venv/bin/python`; everything else is the same.
